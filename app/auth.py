"""
auth.py — 登录认证 / 用户管理 / 权限
====================================================================
设计要点（2026-10-14 新增，为 NAS + Cloudflare 公网访问做准备）：
  · 密码：PBKDF2-HMAC-SHA256（标准库，20 万次迭代），每个用户独立随机盐，
          存成 `pbkdf2_sha256$迭代数$盐$散列`，**绝不存明文**。
  · 会话：HMAC-SHA256 签名的 Cookie（`base64(payload).签名`），带过期时间；
          HttpOnly + SameSite=Lax；请求经 HTTPS（Cloudflare 隧道）时自动加 Secure。
          签名密钥取环境变量 APP_SECRET_KEY；没有就随机生成并存进 settings 表（重启不失效）。
  · 角色：admin（全部）/ editor（生成、发布、改业务数据）/ viewer（只读）。
  · 首次启动自动建管理员：用户名取 ADMIN_USERNAME（默认 admin），
          密码取 ADMIN_INIT_PASSWORD；未配置则随机生成并写入 data/管理员初始密码.txt + 日志。
  · 登录失败按 IP + 用户名做简单节流（同一 IP 连续失败 8 次锁 5 分钟），防公网暴力破解。
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException, Request

from db import SessionLocal, User, get_setting, set_setting

logger = logging.getLogger("poster.auth")

SESSION_COOKIE = "poster_session"
SESSION_TTL_HOURS = int(os.getenv("SESSION_TTL_HOURS", "72"))
PBKDF2_ITER = 200_000
ROLES = {"admin": "管理员", "editor": "编辑", "viewer": "只读"}
# 角色可做的事
ROLE_LEVEL = {"viewer": 1, "editor": 2, "admin": 3}

_fail_bucket = {}     # key → [失败次数, 首次失败时间]
FAIL_LIMIT = 8
FAIL_WINDOW = 300     # 秒


# ---------------- 密码 ----------------
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), PBKDF2_ITER)
    return f"pbkdf2_sha256${PBKDF2_ITER}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, iters, salt, want = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(iters))
        return hmac.compare_digest(dk.hex(), want)
    except Exception:
        return False


# ---------------- 会话签名 ----------------
def _secret() -> bytes:
    s = (os.getenv("APP_SECRET_KEY") or "").strip()
    if s:
        return s.encode()
    s = get_setting("app_secret_key", "")
    if not s:
        s = secrets.token_urlsafe(48)
        set_setting("app_secret_key", s)
        logger.info("已生成会话签名密钥并存入数据库（settings.app_secret_key）")
    return s.encode()


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(_secret(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def make_session_token(uid: int, username: str, role: str) -> str:
    payload = {"uid": uid, "u": username, "r": role, "exp": int(time.time()) + SESSION_TTL_HOURS * 3600}
    raw = json.dumps(payload, separators=(",", ":")).encode()
    b = base64.urlsafe_b64encode(raw).decode().rstrip("=")
    return f"{b}.{_sign(raw)}"


def parse_session_token(token: str):
    try:
        b, sig = (token or "").split(".", 1)
        pad = "=" * (-len(b) % 4)
        raw = base64.urlsafe_b64decode(b + pad)
        if not hmac.compare_digest(sig, _sign(raw)):
            return None
        data = json.loads(raw)
        if int(data.get("exp", 0)) < time.time():
            return None
        return data
    except Exception:
        return None


# ---------------- 请求上下文 ----------------
def client_ip(request: Request) -> str:
    """Cloudflare 隧道下真实客户端 IP 在 CF-Connecting-IP / X-Forwarded-For 里"""
    for h in ("cf-connecting-ip", "x-real-ip", "x-forwarded-for"):
        v = (request.headers.get(h) or "").strip()
        if v:
            return v.split(",")[0].strip()
    return request.client.host if request.client else ""


def user_agent(request: Request) -> str:
    return (request.headers.get("user-agent") or "")[:300]


def current_user(request: Request):
    """返回登录用户 dict，未登录返回 None。每次请求都会校验用户是否仍启用。"""
    tok = request.cookies.get(SESSION_COOKIE, "")
    data = parse_session_token(tok)
    if not data:
        return None
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == int(data["uid"])).first()
        if not u or not u.is_active:
            return None
        return {"id": u.id, "username": u.username, "name": u.name or u.username,
                "role": u.role, "must_change_pw": bool(u.must_change_pw)}
    finally:
        db.close()


def need_login(request: Request) -> dict:
    u = current_user(request)
    if not u:
        raise HTTPException(401, "未登录或登录已过期")
    return u


def need_role(request: Request, role: str = "editor") -> dict:
    u = need_login(request)
    if ROLE_LEVEL.get(u["role"], 0) < ROLE_LEVEL[role]:
        raise HTTPException(403, f"权限不足（需要 {ROLES.get(role, role)}）")
    return u


# ---------------- 限流 ----------------
def login_allowed(ip: str, username: str) -> bool:
    now = time.time()
    for k in (f"ip:{ip}", f"u:{username}"):
        n, t0 = _fail_bucket.get(k, [0, 0])
        if n >= FAIL_LIMIT and now - t0 < FAIL_WINDOW:
            return False
        if now - t0 >= FAIL_WINDOW:
            _fail_bucket[k] = [0, now]
    return True


def mark_login_fail(ip: str, username: str):
    now = time.time()
    for k in (f"ip:{ip}", f"u:{username}"):
        n, t0 = _fail_bucket.get(k, [0, now])
        if now - t0 >= FAIL_WINDOW:
            n, t0 = 0, now
        _fail_bucket[k] = [n + 1, t0]


def clear_login_fail(ip: str, username: str):
    _fail_bucket.pop(f"ip:{ip}", None)
    _fail_bucket.pop(f"u:{username}", None)


# ---------------- 用户管理 ----------------
def authenticate(username: str, password: str):
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.username == (username or "").strip()).first()
        if not u or not u.is_active or not verify_password(password, u.password_hash):
            return None
        u.last_login_at = datetime.utcnow()
        db.commit()
        return {"id": u.id, "username": u.username, "name": u.name or u.username,
                "role": u.role, "must_change_pw": bool(u.must_change_pw)}
    finally:
        db.close()


def ensure_admin():
    """首次启动创建管理员账号（幂等）"""
    db = SessionLocal()
    try:
        if db.query(User).count() > 0:
            return
        username = (os.getenv("ADMIN_USERNAME") or "admin").strip() or "admin"
        pw = (os.getenv("ADMIN_INIT_PASSWORD") or "").strip()
        generated = False
        if not pw:
            pw = secrets.token_urlsafe(9)
            generated = True
        db.add(User(username=username, name="系统管理员", role="admin",
                    password_hash=hash_password(pw), is_active=True, must_change_pw=1 if generated else 0))
        db.commit()
        if generated:
            try:
                # 写在数据库同目录：容器里就是数据卷，重建容器/升级镜像都不丢；
                # 也避免测试实例把文件写进真实 app/data
                from db import DB_PATH
                p = DB_PATH.parent / "管理员初始密码.txt"
                p.write_text(
                    f"用户名：{username}\n初始密码：{pw}\n\n"
                    "⚠️ 首次登录后请立即修改密码，并删除本文件。\n"
                    f"生成时间：{datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")
            except Exception:
                pass
            logger.warning("=" * 62)
            logger.warning("已创建管理员账号：%s  初始密码：%s", username, pw)
            logger.warning("（同时写入 app/data/管理员初始密码.txt，请首次登录后立即修改）")
            logger.warning("=" * 62)
        else:
            logger.info("已创建管理员账号：%s（使用 ADMIN_INIT_PASSWORD 指定的密码）", username)
    finally:
        db.close()


def change_password(uid: int, old_pw: str, new_pw: str) -> tuple:
    """返回 (ok, msg)"""
    if len(new_pw or "") < 8:
        return False, "新密码至少 8 位"
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            return False, "用户不存在"
        if not verify_password(old_pw or "", u.password_hash):
            return False, "原密码不正确"
        u.password_hash = hash_password(new_pw)
        u.must_change_pw = 0
        db.commit()
        return True, "密码已修改"
    finally:
        db.close()


def public_user(u: User) -> dict:
    return {"id": u.id, "username": u.username, "name": u.name or u.username, "role": u.role,
            "role_label": ROLES.get(u.role, u.role), "is_active": bool(u.is_active),
            "must_change_pw": bool(u.must_change_pw),
            "last_login_at": u.last_login_at.strftime("%Y-%m-%d %H:%M:%S") if u.last_login_at else "",
            "created_at": u.created_at.strftime("%Y-%m-%d %H:%M:%S") if u.created_at else ""}
