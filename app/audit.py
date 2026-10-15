"""
audit.py — 操作日志（审计）
====================================================================
记录谁、什么时候、从哪个 IP、做了什么、结果如何。
用于 NAS + Cloudflare 公网部署后追溯操作（发布文章属于对外动作，必须留痕）。

用法（在接口里一行接入）：
    from audit import audit
    audit(request, "login", "登录成功", user=u)                  # 登录
    audit(request, "user.create", f"新建用户 {name}（角色 editor）", user=admin_u)
    audit(request, "publish.run", f"创建发布任务 {job_id}，共 {n} 篇", user=u, level="warn")

约定 action 命名：`模块.动作`，如 auth.login / user.create / job.run / publish.done / settings.schedule。
"""
import csv
import io
import logging
from datetime import datetime

from db import SessionLocal, AuditLog

logger = logging.getLogger("poster.audit")

# 常用动作 → 中文说明（前端展示用）
ACTION_LABEL = {
    "auth.login": "登录",
    "auth.login_fail": "登录失败",
    "auth.logout": "退出登录",
    "auth.password": "修改密码",
    "user.create": "新增用户",
    "user.update": "修改用户",
    "user.delete": "删除用户",
    "user.resetpw": "重置密码",
    "job.run": "创建发布任务",
    "job.daily": "手动补跑每日批次",
    "settings.schedule": "修改定时设置",
    "settings.llm": "修改模型设置",
    "account.create": "新增公众号",
    "account.delete": "删除公众号",
    "product.create": "新增产品",
    "product.update": "修改产品",
    "product.delete": "删除产品",
    "topic.create": "新增主题",
    "topic.update": "修改主题",
    "topic.delete": "删除主题",
}


def audit(request=None, action: str = "", detail: str = "", user=None,
          result: str = "ok", level: str = "info", extra: str = "") -> None:
    """写一条操作日志。任何异常都不能影响主流程，因此整体 try 包裹。"""
    try:
        u = user
        if u is None and request is not None:
            try:
                import auth
                u = auth.current_user(request)
            except Exception:
                u = None
        ip, ua = "", ""
        if request is not None:
            try:
                import auth
                ip = auth.client_ip(request)
                ua = auth.user_agent(request)
            except Exception:
                pass
        db = SessionLocal()
        try:
            db.add(AuditLog(
                ts=datetime.utcnow(),
                user_id=(u or {}).get("id") if u else None,
                username=(u or {}).get("username", "") if u else (detail or "")[:32],
                role=(u or {}).get("role", "") if u else "",
                action=action, detail=detail[:1000], result=result, level=level,
                ip=ip, ua=ua, extra=extra[:500]))
            db.commit()
        finally:
            db.close()
    except Exception as e:      # noqa: BLE001
        logger.warning("写操作日志失败（不影响主流程）：%s", str(e)[:150])


def query_logs(limit: int = 100, offset: int = 0, q: str = "", action: str = "",
               username: str = "", level: str = "") -> dict:
    db = SessionLocal()
    try:
        qs = db.query(AuditLog)
        if q:
            like = f"%{q}%"
            qs = qs.filter((AuditLog.detail.like(like)) | (AuditLog.username.like(like))
                           | (AuditLog.ip.like(like)) | (AuditLog.action.like(like)))
        if action:
            qs = qs.filter(AuditLog.action == action)
        if username:
            qs = qs.filter(AuditLog.username == username)
        if level:
            qs = qs.filter(AuditLog.level == level)
        total = qs.count()
        rows = qs.order_by(AuditLog.id.desc()).offset(max(0, offset)).limit(min(max(1, limit), 500)).all()
        return {"total": total, "items": [{
            "id": r.id,
            "ts": (r.ts + __import__("datetime").timedelta(hours=8)).strftime("%Y-%m-%d %H:%M:%S") if r.ts else "",
            "username": r.username or "-", "role": r.role or "",
            "action": r.action, "action_label": ACTION_LABEL.get(r.action, r.action),
            "detail": r.detail or "", "result": r.result, "level": r.level,
            "ip": r.ip or "", "ua": (r.ua or "")[:120],
        } for r in rows]}
    finally:
        db.close()


def export_csv(limit: int = 5000) -> str:
    data = query_logs(limit=limit)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["时间", "用户", "角色", "操作", "说明", "结果", "级别", "IP", "User-Agent"])
    for it in data["items"]:
        w.writerow([it["ts"], it["username"], it["role"], it["action_label"], it["detail"],
                    it["result"], it["level"], it["ip"], it["ua"]])
    return "\ufeff" + buf.getvalue()      # BOM：Excel 打开不乱码
