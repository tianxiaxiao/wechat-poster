"""
notify.py — 任务完成通知（企业微信群机器人 Webhook，推到手机微信）
用法：
  1. 企业微信群 → 右键群 → 添加「群机器人」→ 复制 Webhook URL（含 key）
  2. 配置环境变量 ROBOT_WEBHOOK_URL=https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxxx
  3. 任务（每日定时/勾选了通知的手动发布）跑完 → 自动把结果推送到该群，手机微信可见
说明：纯标准库 urllib，无第三方依赖；通知失败只记日志，绝不影响发布主流程。
"""
import json
import logging
import os
import re
import time
import urllib.request

log = logging.getLogger("poster.notify")


def safe_article_url(url: str) -> str:
    """确保是「能打开的完整文章链接」。

    ⚠️ 踩坑记录（2026-10-05）：公众号文章**没有可自行拼接的短链**。
    实测 https://mp.weixin.qq.com/s?__biz=..&mid=..&idx=1&sn=..&chksm=.. 是唯一可用形式：
      · 去掉 chksm 参数 → 打开是「参数错误」页
      · 自己拼 https://mp.weixin.qq.com/s/{sn} → 同样是「参数错误」页
    （页面里出现的 /s/xxxx 短码是微信自己的帮助页链接，与文章无关）
    因此这里只做「补全协议头」的兜底，绝不裁剪参数。
    """
    u = (url or "").strip()
    if u.startswith("http://"):
        u = "https://" + u[len("http://"):]
    return u


def build_summary_lines(items: list, with_theme: bool = True, title_max: int = 0) -> list:
    """把 [{'title','url','topic','status','theme'}] 渲染成 markdown 明细行。
    标题本身就是链接（比再加「查看」二字省字节）：`- ✅ [标题](完整链接)（主题）`。"""
    lines = []
    for it in items:
        st = it.get("status") or "published"
        mark = "✅" if st == "published" else "❌"
        t = (it.get("title") or it.get("topic") or "").strip()
        if title_max and len(t) > title_max:
            t = t[:title_max] + "…"
        theme = (it.get("theme") or "").strip()
        suffix = f"（{theme}）" if (with_theme and theme) else ""
        u = safe_article_url(it.get("url") or "")
        lines.append(f"- {mark} [{t}]({u}){suffix}" if u else f"- {mark} {t}{suffix}")
    return lines


def _byte_truncate(s: str, limit: int) -> str:
    """按 UTF-8 字节数截断（不切断多字节字符）。微信限制按字节计，中文 1 字=3 字节。"""
    b = s.encode("utf-8")
    if len(b) <= limit:
        return s
    return b[:limit].decode("utf-8", errors="ignore")


def robot_url() -> str:
    return (os.getenv("ROBOT_WEBHOOK_URL") or "").strip()


def configured() -> bool:
    return bool(robot_url())


def _post(content: dict, timeout: int = 15) -> dict:
    url = robot_url()
    if not url:
        return {"errcode": -1, "errmsg": "未配置 ROBOT_WEBHOOK_URL"}
    data = json.dumps(content, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def send(title: str, body: str = "", use_markdown: bool = False) -> dict:
    """推送一条消息。use_markdown=True 时用 markdown（支持 [标题](链接) 可点击）。
    ⚠️ 微信按【字节】限长：markdown 4096、text 2048；超限会整条被拒，这里做字节安全截断。
    明细较多时请改用 send_markdown_chunks() 自动分片，避免丢信息。"""
    try:
        if use_markdown:
            content = _byte_truncate(title + ("\n" + body if body else ""), 4000)
            r = _post({"msgtype": "markdown", "markdown": {"content": content}})
        else:
            content = _byte_truncate(title + ("\n" + body if body else ""), 1900)
            r = _post({"msgtype": "text", "text": {"content": content}})
        if r.get("errcode", -1) != 0:
            log.warning("群机器人通知返回错误: %s", r)
        return r
    except Exception as e:  # noqa: BLE001
        log.warning("群机器人通知异常: %s", str(e)[:150])
        return {"errcode": -1, "errmsg": str(e)[:100]}


def send_markdown_chunks(head: str, lines: list, limit_bytes: int = 3600) -> list:
    """把「标题 + 明细行」按字节上限自动分片发送（超出微信 4096 字节限制时拆成多条，
    每条标注 (i/n)），确保任何篇数都不丢信息、每篇链接都在。返回各次发送结果。"""
    head_b = len(head.encode("utf-8")) + 30   # 预留分片后缀空间
    budget = max(limit_bytes - head_b, 600)
    chunks, cur, cur_b = [], [], 0
    for ln in lines:
        b = len(ln.encode("utf-8")) + 1
        if cur and cur_b + b > budget:
            chunks.append(cur); cur, cur_b = [], 0
        cur.append(ln); cur_b += b
    if cur:
        chunks.append(cur)
    if not chunks:
        return [send(head, "", use_markdown=True)]
    n = len(chunks)
    results = []
    for i, c in enumerate(chunks):
        h = head if n == 1 else f"{head}（{i + 1}/{n}）"
        results.append(send(h, "\n".join(c), use_markdown=True))
        if i < n - 1:
            time.sleep(0.6)   # 轻微间隔，避免触发机器人 20条/分钟 限速
    log.info("通知分片发送完成：共 %d 条", n)
    return results


def send_daily_summary(head: str, items: list, tail: str = "",
                       limit_bytes: int = 3900) -> list:
    """推送当天汇总：**优先一条**，装不下才分条（每条标注 1/n）。

    为什么会有分条：企业微信 markdown 单条上限 4096 字节，而公众号文章**只有完整长链接可用**
    （189 字节/篇，去参数或自己拼短链都会「参数错误」）。
    20 篇的链接本身就 3780 字节 → 加标题必然超限，属平台硬限制，无法单条承载。
    篇数少的日子（约 ≤10 篇）会自动只发一条。
    """
    lines = build_summary_lines(items)
    content = head + "\n\n" + "\n".join(lines) + (("\n\n" + tail) if tail else "")
    if len(content.encode("utf-8")) <= limit_bytes:
        log.info("每日汇总单条发送（%d 字节，%d 篇）", len(content.encode("utf-8")), len(items))
        return [send(content, "", use_markdown=True)]
    log.info("每日汇总 %d 篇、%d 字节 → 超单条上限，分条发送（含文章链接，不裁信息）",
             len(items), len(content.encode("utf-8")))
    return send_markdown_chunks(head, lines)


def send_job_summary(job: dict) -> dict:
    """任务跑完后推送结果摘要（markdown，每篇附文章链接便于直接查看）。"""
    arts = job.get("articles", []) or []
    total = len(arts)
    ok = sum(1 for a in arts if a.get("status") == "published")
    failed = [a for a in arts if a.get("status") != "published"]
    src = job.get("source_label") or ""
    tag = "⏰ 每日定时" if "定时" in src else ("手动" if src == "手动" else "任务")
    title = f"## 📰 润致自动发布·{tag}完成：成功 {ok} / 共 {total} 篇"
    lines = []
    for a in arts:
        st = a.get("status")
        mark = "✅" if st == "published" else ("❌" if st == "failed" else f"⏳{st}")
        t = (a.get("title") or "").strip() or f"第{int(a.get('seq', 0)) + 1}篇（{a.get('topic', '')}）"
        # 卡片排版主题（如「字节范」）→ 与每日汇总（build_summary_lines）格式保持一致；
        # 扫码发布不经过卡片排版、无主题，则不加后缀。
        theme = (a.get("theme") or "").strip()
        suffix = f"（{theme}）" if theme else ""
        url = (a.get("url") or "").strip()
        lines.append(f"- {mark} [{t[:50]}]({url}){suffix}" if url else f"- {mark} {t[:60]}{suffix}")
    if failed:
        lines.append(f"\n⚠️ 未成功 {len(failed)} 篇，可在网页任务列表查看原因")
    elif total:
        lines.append("\n全部成功，点标题可查看文章。")
    body = "\n".join(lines)
    log.info("发送任务完成通知: 成功%d/%d", ok, total)
    return send_markdown_chunks(title, lines)


def send_test() -> dict:
    """连通性测试：推一条测试消息。"""
    return send("🔔 润致公众号自动发布 · 通知通道测试",
                "如果你在微信里看到这条消息，说明群机器人通知配置成功 ✅\n"
                "以后每天定时任务跑完，结果会自动推到这里。")


if __name__ == "__main__":
    print(send_test())
