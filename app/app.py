"""
润致公众号自动发布工具 — Web App 主入口
流程：填主题 → 后台调 LLM(DeepSeek等) 生成 → 发布
  模式 mode=scan: 串行调用 publisher.js → 用户扫码发表（文章进主页/群发）
  模式 mode=api:  官方 freepublish/submit → 全自动发布到"发表记录"（不推送、免扫码）
"""
import os
import re
import json
import uuid
import threading
import time
import logging
from pathlib import Path
from typing import Dict, Optional
from datetime import datetime, timedelta
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

from fastapi import FastAPI, BackgroundTasks, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from db import (init_db, Article, Account, Product, Topic, SessionLocal, get_setting, set_setting,
                User, AuditLog, UserSetting)
import db as db_api
import auth
import audit
import llm
import dedup
import notify
import publisher as wx
import wechat_api
import wxcard

APP_DIR = Path(__file__).parent
DATA_DIR = APP_DIR / "data"
ARTICLES_DIR = DATA_DIR / "articles"
QR_DIR = DATA_DIR / "qrcodes"
LOG_DIR = DATA_DIR / "logs"
for d in (DATA_DIR, ARTICLES_DIR, QR_DIR, LOG_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 嗅探图片真实 mime
def _sniff_media(path):
    try:
        head = path.read_bytes()[:64]
    except Exception:
        return "image/png"
    if head.startswith(b"\x89PNG"): return "image/png"
    if head[:3] == b"\xff\xd8\xff": return "image/jpeg"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP": return "image/webp"
    if head[:6] in (b"GIF87a", b"GIF89a"): return "image/gif"
    # SVG: 以 '<' 或 '<?xml' 开头（去掉前导空白）
    stripped = head.lstrip()
    if stripped.startswith(b"<svg") or stripped.startswith(b"<?xml"):
        return "image/svg+xml"
    return "image/png"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("poster")

# 内存任务状态
jobs: Dict[str, dict] = {}
jobs_lock = threading.Lock()


def _ensure_card_render_ok(where: str = "") -> bool:
    """卡片排版自检：渲染一小段测试文本，确认 md2card 管线可用。

    背景（2026-10-06）：托管 node 从 22.22.2-3 升到 -6，硬编码路径失效 → 渲染静默降级回
    旧版 md_to_html，整批 20 篇都变成旧样式且无人察觉。现在启动时与每批开始前都自检，
    失败立即企微告警（当天只告警一次），避免再次静默降级。
    """
    try:
        html = wxcard.render_md_to_card_html("# 自检\n\n这是排版管线自检。", "alibaba")
        if not html or "<section" not in html:
            raise RuntimeError("自检输出不含卡片结构")
        logger.info("卡片排版自检通过（%s，node=%s）", where, wxcard.find_node().name)
        return True
    except Exception as e:
        logger.error("卡片排版自检失败（%s）：%s", where, str(e)[:200])
        try:
            today = datetime.now().strftime("%Y-%m-%d")
            if get_setting("render_selftest_alert", "") != today and notify.configured():
                set_setting("render_selftest_alert", today)
                notify.send("## ⚠️ 卡片排版不可用，文章已降级为旧版排版",
                            f"自检失败：`{str(e)[:150]}`\n"
                            "影响：文章仍能正常发布，但不再使用 md2card 卡片样式（且旧版分隔线可能在客户端显示多余斜杠）。\n"
                            "排查：检查 node 是否可用（`wxcard.find_node()`），或设置环境变量 WX_NODE 指定 node 路径。",
                            use_markdown=True)
        except Exception:
            pass
        return False


def _add_draft_with_cover_retry(title: str, body_html: str, author: str, digest: str = "",
                                tries: int = 3) -> tuple:
    """存草稿；封面裁剪失败（errcode 53402）时换一张封面立即重试。

    背景：随机封面图偶发裁剪失败（2026-10-01 第 11 篇即因此失败，被留到 15 分钟后的下一批）。
    这里换成另一张封面立刻重试，最多 tries 次，避免为一篇的封面问题白等一整批。
    返回 (media_id, 备注文案)；全部失败则抛最后一次异常。
    """
    last_err = None
    for i in range(max(1, tries)):
        thumb = wechat_api.pick_random_cover_media()
        try:
            mid = wechat_api.add_draft(title=title, content_html=body_html, author=author,
                                       thumb_media_id=thumb, digest=digest)
            note = f"⚠️ 封面裁剪失败已换封面重试成功（第 {i+1} 次）" if i > 0 else ""
            return mid, note
        except Exception as e:
            last_err = e
            msg = str(e)
            if "53402" in msg or "封面" in msg:
                logger.warning("封面裁剪失败（第 %d 次），换封面重试：%s", i + 1, msg[:100])
                continue
            raise
    raise last_err if last_err else RuntimeError("add_draft 失败")


# ---- LLM 探活等待（2026-10-15 新增）----
# 背景：DeepSeek 曾在无人值守批次中途 503（Service too busy），程序照跑导致整批文章失败。
# 策略：每篇生成前先探活；不通则按 30s→60s→120s→300s 退避等待，恢复后继续生成；
#       最长等 LLM_WAIT_MAX_MIN 分钟（默认 120，可用环境变量覆盖），超时才判本篇失败。
#       鉴权错误（401/403/无效 key）等待无意义 → 立即失败并给出明确提示。
LLM_WAIT_MAX_MIN = int(os.getenv("LLM_WAIT_MAX_MIN", "120"))
_LLM_RECHECK_SEC = [30, 60, 120, 300]


def _is_llm_auth_error(err: str) -> bool:
    s = (err or "").lower()
    return ("401" in s or "403" in s or "unauthorized" in s
            or "authentication" in s or "invalid api key" in s
            or "incorrect api key" in s)


def _llm_wait_ready(job: dict, log_sink: list) -> bool:
    """阻塞直到 LLM 探活成功；超时/鉴权失败返回 False（调用方据此判本篇失败）。"""
    provider = job.get("provider", "deepseek")
    deadline = time.time() + LLM_WAIT_MAX_MIN * 60
    attempt = 0
    while True:
        # 用户中途点「取消」→ 立即中止等待，本篇生成判失败（由调用方安全收尾）
        if job.get("cancel"):
            log_sink.append("  ⛔ 用户已取消（等待 LLM 恢复期间），中止等待")
            return False
        ok, err = llm.ping(provider, api_key=job.get("api_key", ""),
                           base_url=job.get("base_url", ""), model=job.get("llm_model", ""),
                           allow_env_key=bool(job.get("allow_env_key", True)))
        if ok:
            if attempt:
                log_sink.append(f"  ✅ LLM 已恢复（共等待 {attempt} 轮），继续生成")
            return True
        if _is_llm_auth_error(err):
            log_sink.append(f"  ❌ LLM 鉴权失败（{err[:140]}），等待无法解决 → 本篇判失败")
            return False
        if time.time() >= deadline:
            log_sink.append(f"  ❌ LLM 持续不可用超过 {LLM_WAIT_MAX_MIN} 分钟，放弃等待 → 本篇判失败（{err[:140]}）")
            return False
        wait = _LLM_RECHECK_SEC[min(attempt, len(_LLM_RECHECK_SEC) - 1)]
        attempt += 1
        log_sink.append(f"  ⏳ LLM 探活失败（{err[:100]}）→ {wait}s 后重试（第 {attempt} 轮，最长等待 {LLM_WAIT_MAX_MIN} 分钟）")
        # 拆分 sleep 为 3s 步长并轮询 cancel，保证点「取消」后数秒内即响应（不再干转数分钟）
        slept = 0
        while slept < wait:
            if job.get("cancel"):
                log_sink.append("  ⛔ 用户已取消（等待 LLM 恢复期间），中止等待")
                return False
            time.sleep(min(3, wait - slept))
            slept += 3


# ---- 按用户解析 LLM 配置（多租户 + 管理员授权）2026-10-16 ----
# 规则（用户明确要求）：
#   · 每位用户默认**必须用自己的 API Key**，各自消耗各自的额度；
#   · 只有【管理员本人】或【被管理员授权 llm_use_shared=1 的用户】才允许回落到共享 Key
#     （管理员「我的设置」里的 Key → 服务端 .env 的默认 Key）；
#   · 未授权又没填 Key，且该 provider 需要 Key → **硬拦**（绝不静默用服务端默认 Key）；
#   · 本地/自建模型（ollama、本机/内网地址、自己的网关域名）本来不校验 Key → 不拦。
_GRANT_HINT = "，或让管理员在「👥 用户管理」里勾选「授权共享模型」"


def _resolve_llm_for_user(user_id, *, provider="", base_url="", model="", api_key="",
                          is_admin=None, db=None):
    """返回 (cfg, err)。cfg 含 provider/base_url/model/api_key/key_source/using_shared/requires_key；
    err 非空表示**不允许调用**（未授权且缺 Key），调用方必须拒绝并展示该提示。
    取值优先级：请求显式传入 > 该用户自己的设置 > （被授权时）管理员共享 > provider 内置默认。"""
    _close = False
    if db is None:
        db = SessionLocal(); _close = True
    try:
        u = db.query(User).filter(User.id == user_id).first() if user_id else None
        if is_admin is None:
            is_admin = bool(u and u.role == "admin")
        us = db_api.get_user_setting(user_id, db) if user_id else None
        share = db_api.get_shared_llm_setting(db)
        using_shared = bool(is_admin or (us and us.llm_use_shared))
    finally:
        if _close:
            db.close()

    def pick(*vals):
        for v in vals:
            if v and str(v).strip():
                return str(v).strip()
        return ""

    sh_prov = share.llm_provider if (using_shared and share) else ""
    sh_base = share.llm_base_url if (using_shared and share) else ""
    sh_model = share.llm_model if (using_shared and share) else ""
    prov = pick(provider, us.llm_provider if us else "", sh_prov, "deepseek")
    if prov not in llm.PROVIDERS:
        prov = "deepseek"
    base_url = pick(base_url, us.llm_base_url if us else "", sh_base)
    model = pick(model, us.llm_model if us else "", sh_model)
    if not base_url:
        base_url = llm.PROVIDERS[prov]["default_base_url"]
    if not model:
        model = llm.PROVIDERS[prov]["default_model"]

    requires_key = llm.requires_user_key(prov, base_url)
    own_key = pick(api_key, us.llm_api_key if us else "")
    shared_key = ""
    if using_shared:
        shared_key = (share.llm_api_key if share and share.llm_api_key else "") or llm.server_default_key(prov)
    shared_from_admin = bool(share and share.llm_api_key)

    cfg = {"provider": prov, "base_url": base_url, "model": model, "api_key": "",
           "key_source": "", "using_shared": using_shared, "requires_key": requires_key,
           "allow_env_key": using_shared}
    if own_key:
        cfg.update(api_key=own_key, key_source="你自己的 Key")
        return cfg, ""
    if not requires_key:
        # 本地/自建模型通常不校验 Key：即使借用共享 Key 也只用于自家网关，且不留凭证给用户
        if using_shared and shared_key:
            cfg.update(api_key=shared_key,
                       key_source="管理员共享" if shared_from_admin else "管理员共享（服务端默认）")
        else:
            cfg.update(key_source="不需要（本地/自建模型）")
        return cfg, ""
    if using_shared and shared_key:
        cfg.update(api_key=shared_key,
                   key_source="管理员共享" if shared_from_admin else "管理员共享（服务端默认）")
        return cfg, ""
    if is_admin:
        err = (f"服务端未配置 {prov} 的默认 API Key，且你也没有填写自己的 Key —— "
               f"请在「⚙️ AI 模型设置」里填写你自己的 Key")
    else:
        err = (f"未设置你自己的 API Key（当前模型来源：{prov}）—— 多用户模式下**不能使用服务端默认 Key**。"
               f"请在「⚙️ AI 模型设置」里填写**你自己的** Key{_GRANT_HINT}")
    return cfg, err


def _gen_with_dedup(topic: str, count: int, job: dict, log_sink: list,
                    extra_history: Optional[list] = None) -> list:
    """带历史相似度去重的 LLM 生成（两层防护）：
    1) 提示词层：把本地库最近的历史标题注入 prompt，要求完全不同的句式/角度；
    2) 后过滤层：生成结果逐篇与历史做 bigram Jaccard 相似度比对（标题>0.5 或正文开头>0.6 判雷同），
       命中后自动换角度重写一次；重写结果相似度更低则采用重写版，否则保留原稿并记录警告。
    extra_history：本次任务中前面主题已定稿的文章，也参与后续主题的比对。"""
    history = dedup.load_history() + [h for h in (extra_history or [])]
    history_titles = [h["title"] for h in history if h.get("title")]
    # 生成前先探活：LLM 挂了就等（30s→60s→120s→300s 退避），恢复后继续 —— 无人值守批次不再因
    # DeepSeek 临时故障（如 503 Service too busy）把后续篇全部判死。
    if not _llm_wait_ready(job, log_sink):
        raise RuntimeError("LLM 不可用（探活超时或鉴权失败），本篇生成中止")
    # 本地/自建模型（ollama / openai_compat / 局域网或自建域名）→ 逐篇生成：一次只请求 1 篇。
    # 在任务日志里明示，便于与云端模型的批量生成区分、排查"生成不出/响应慢"。
    if count > 1 and llm.is_local_llm(job.get("provider"), job.get("base_url")):
        log_sink.append(f"  🔧 本地/自建模型：『{topic}』{count} 篇将【逐篇生成】"
                        f"（每次只请求 1 篇，避免一次要多篇导致响应不及时/超时/截断）")
    items = []
    for _try in range(3):   # 生成中途遇服务故障 → 等待恢复后重试（最多 3 次尝试）
        try:
            items = llm.generate_articles(
                topic, count, brand=job["brand"], extra_hint=job.get("extra_hint", ""),
                provider=job.get("provider", "deepseek"),
                api_key=job.get("api_key", ""),
                base_url=job.get("base_url", ""),
                model=job.get("llm_model", ""),
                product_name=job.get("product_name", ""),
                product_intro=job.get("product_intro", ""),
                product_style=job.get("product_style", ""),
                history_titles=history_titles or None,
                allow_env_key=bool(job.get("allow_env_key", True)),   # 未获管理员授权 → 绝不用服务端 Key
            )
            break
        except Exception as e:
            if _try == 2 or _is_llm_auth_error(str(e)):
                raise
            log_sink.append(f"  ⚠️ 生成失败（{str(e)[:100]}）→ 等待 LLM 恢复后重试（第 {_try + 2}/3 次尝试）")
            if not _llm_wait_ready(job, log_sink):
                raise
    accepted = []
    for it in items:
        r = dedup.check_article(it.get("title", ""), it.get("body_md", ""), history)
        if r["ok"]:
            accepted.append(it)
            continue
        # 命中雷同 → 换角度重写一次
        why = []
        if r["title_sim"] >= dedup.TITLE_SIM_THRESHOLD:
            why.append(f"标题与已发《{r['matched_title']}》相似度 {r['title_sim']:.0%}")
        if r["body_sim"] >= dedup.BODY_SIM_THRESHOLD:
            why.append(f"正文与已发《{r['matched_body_title']}》相似度 {r['body_sim']:.0%}")
        log_sink.append(f"  ⚠️《{it.get('title','')}》疑似与历史文章撞车（{'；'.join(why)}）→ 自动换角度重写…")
        logger.info("dedup 触发重写: title=%s %s", it.get("title", ""), "；".join(why))
        retry_hint = (f"刚才写的那篇与已发布文章撞车了（{';'.join(why)}）。"
                      f"必须彻底换一个人物身份、场景和叙事顺序重写，标题用完全不同的公式。")
        try:
            retry = llm.generate_articles(
                topic, 1, brand=job["brand"],
                extra_hint=(job.get("extra_hint", "") + "；" if job.get("extra_hint") else "") + retry_hint,
                provider=job.get("provider", "deepseek"),
                api_key=job.get("api_key", ""),
                base_url=job.get("base_url", ""),
                model=job.get("llm_model", ""),
                product_name=job.get("product_name", ""),
                product_intro=job.get("product_intro", ""),
                product_style=job.get("product_style", ""),
                history_titles=history_titles or None,
                allow_env_key=bool(job.get("allow_env_key", True)),   # 未获管理员授权 → 绝不用服务端 Key
            )
        except Exception as e:
            retry = []
            log_sink.append(f"  重写失败（{str(e)[:80]}），保留原稿")
        if retry:
            r2 = dedup.check_article(retry[0].get("title", ""), retry[0].get("body_md", ""), history)
            sim1 = max(r["title_sim"] / dedup.TITLE_SIM_THRESHOLD, r["body_sim"] / dedup.BODY_SIM_THRESHOLD)
            sim2 = max(r2["title_sim"] / dedup.TITLE_SIM_THRESHOLD, r2["body_sim"] / dedup.BODY_SIM_THRESHOLD)
            if sim2 < sim1:
                it = retry[0]
                log_sink.append(f"  ✅ 重写通过：《{it.get('title','')}》")
            else:
                log_sink.append(f"  重写仍偏高（{r2['title_sim']:.0%}），保留相似度更低的一版继续")
        accepted.append(it)
        # 定稿的也进入比对池，供同批后续主题/篇目参考
        history.append({"title": it.get("title", ""), "body": it.get("body_md", "")})
    return accepted


def _new_job(topics: list, brand: str, author: str, extra_hint: str = "",
             operator: dict = None) -> dict:
    """topics: [{name, count}]，每主题独立生成 count 篇，全局 seq 连续。
    operator: 发起人（{id, username, role}），用于操作日志留痕（定时任务为 None=系统）。"""
    job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    total = sum(t["count"] for t in topics)
    return {
        "id": job_id,
        "topics": topics,
        "topic": topics[0]["name"] if topics else "",   # 兼容展示：首个主题名
        "total": total,
        "count": total,                                  # 兼容旧逻辑：总篇数
        "brand": brand,
        "author": author,
        "extra_hint": extra_hint,
        "operator": operator or {},
        "created_at": datetime.now().isoformat(),
        "phase": "queued",     # queued / generating / publishing_p1 / .../ done / failed / cancelled
        "articles": [],        # [{seq, topic, title, status, qr_url, ok, error, publish_id}]
        "cancel": False,       # 用户在前端点「取消」后置 True，worker 在下个检查点安全收尾
        "log": [],
    }


def _run_job(job_id: str):
    """后台 worker：调 LLM → 串行发布"""
    job = jobs.get(job_id)
    if not job:
        return
    db = SessionLocal()
    try:
        # ===== 阶段1：LLM 生成（遍历多主题，每个主题生成其 count 篇，全局 seq 连续） =====
        with jobs_lock:
            job["phase"] = "generating"
        topics_desc = "、".join(f"『{t['name']}』×{t['count']}" for t in job["topics"])
        job["log"].append(f"[{datetime.now().isoformat()}] 开始生成：{topics_desc}（provider={job.get('provider','deepseek')}, model={job.get('llm_model','-')}）")
        gen_ok_all = True
        global_seq = 0
        accepted_history: list = []   # 本任务内已定稿文章 → 参与后续主题的相似度比对
        try:
            for t in job["topics"]:
                if job.get("cancel"):
                    gen_ok_all = False
                    with jobs_lock:
                        job["log"].append("⛔ 用户已取消（生成阶段），停止后续主题生成")
                    break
                tname, tcount = t["name"], t["count"]
                job["log"].append(f"  生成『{tname}』{tcount} 篇…（含历史相似度去重）")
                items = _gen_with_dedup(tname, tcount, job, job["log"],
                                        extra_history=accepted_history)
                for item in items:
                    title = item["title"]
                    body = item["body_md"]
                    seq = global_seq
                    global_seq += 1
                    job["articles"].append({"seq": seq, "topic": tname, "title": title, "status": "generated", "body": body[:200]})
                    # 写正文到磁盘
                    md_path = ARTICLES_DIR / f"{job_id}_{seq}.md"
                    md_path.write_text(body, encoding="utf-8")
                    # 写 DB
                    art = Article(
                        job_id=job_id, seq=seq, topic=tname,
                        title=title, body_md=body, status="generated",
                    )
                    db.add(art)
                db.commit()
                # 定稿文章加入本任务比对池（后续主题不再与之撞车）
                accepted_history.extend({"title": it["title"], "body": it["body_md"]} for it in items)
                job["log"].append(f"  生成『{tname}』{len(items)} 篇：{[it['title'] for it in items]}")
        except Exception as e:
            gen_ok_all = False
            with jobs_lock:
                job["phase"] = "failed"
                job["log"].append(f"LLM 调用失败: {e}")
            for seq in range(job["total"]):
                if seq < len(job["articles"]):
                    continue
                job["articles"].append({"seq": seq, "topic": "", "status": "failed", "error": str(e)})
            return

        if job.get("cancel"):
            with jobs_lock:
                job["phase"] = "cancelled"
                job["log"].append("⛔ 任务已取消（生成阶段），未进入发布。可在「定时/补发」里重试未完成的篇。")
            return
        if not gen_ok_all:
            return
        job["log"].append(f"生成成功，共 {global_seq} 篇。")
        # 用实际生成数修正 total（异常主题可能少篇）
        job["total"] = global_seq
        job["count"] = global_seq

        # ===== 阶段2：串行发布 =====
        use_api = bool(job.get("use_api"))
        # 目标公众号（API 发布会切到该 appid/secret；扫码模式只能用 playwright 登录的号）
        target_appid = job.get("account_appid") or ""
        target_secret = job.get("account_secret") or ""
        # playwright 登录的固定号 appid（只有这个号能在 UI 存带原创+赞赏的草稿）
        # 从环境变量读：WX_LOGIN_APPID 优先，其次 WX_APPID；都不配则扫码模式不可用
        LOGIN_APPID = os.getenv("WX_LOGIN_APPID") or os.getenv("WX_APPID", "")
        hit_limit = False   # 命中微信频率限制(53501) → 停止本轮剩余发布，避免连撞加重冷却
        for seq in range(job["count"]):
            # 发布阶段也支持中途取消：用户点「取消」后，本篇之前已发布的保留，剩余篇不再处理
            if job.get("cancel"):
                with jobs_lock:
                    job["log"].append("⛔ 用户已取消（发布阶段），剩余篇未处理（已发布的可正常保留）")
                break
            # 手动批量发布同样要拉开间隔（实测微信要求提交间隔≥约30分钟才放行；
            # 这里至少保持 POST_INTERVAL_SEC 秒，避免密集提交触发惩罚性限流）
            if seq > 0:
                time.sleep(POST_INTERVAL_SEC)
                with jobs_lock:
                    job["log"].append(f"  已等待 {POST_INTERVAL_SEC}s（微信频控保护），继续第 {seq+1} 篇…")
            with jobs_lock:
                job["phase"] = f"publishing_p{seq+1}"
                job["log"].append(f"[{datetime.now().isoformat()}] 开始发布第 {seq+1} 篇…（方式: {'API 全自动' if use_api else '扫码发表'}｜号: {job.get('account_name','默认')}）")
            article = db.query(Article).filter(Article.job_id == job_id, Article.seq == seq).first()
            md_path = ARTICLES_DIR / f"{job_id}_{seq}.md"
            article.status = "publishing"
            db.commit()
            job["articles"][seq]["status"] = "publishing"

            if use_api:
                # ---------- API 全自动发布（切换到所选公众号） ----------
                mid = ""
                try:
                    if target_appid and target_secret:
                        wechat_api.set_account(target_appid, target_secret)
                        job["log"].append(f"  第 {seq+1} 篇：切换到公众号 appid={target_appid[:8]}…")
                    # 统一走「纯 API」富排版发布（任何账号都适用）：draft/add(富HTML) → freepublish/submit
                    # 卡片排版：复刻 md2card「公众号模式」管线（5 套公众号专用样式，CSS 内联，
                    # 微信白名单兼容）；自动发布时在 4 个主题里随机选（不含日本杂志）；失败降级旧版
                    job["log"].append(f"  第 {seq+1} 篇：生成卡片排版 HTML（随机主题）…")
                    body_html, _tmid, _tmname = wxcard.render_md_with_theme_safe(md_path.read_text(encoding="utf-8"))
                    job["log"].append(f"  第 {seq+1} 篇：排版主题「{_tmname}」")
                    # 主题名落 job + DB：手动发布完成通知要显示「（字节范）」，与每日汇总保持一致；
                    # 落库还能让「手动发的文章」在当天定时汇总里也带上主题（汇总优先读库）。
                    job["articles"][seq]["theme"] = _tmname
                    article.theme = _tmname
                    db.commit()
                    # 存草稿（封面裁剪失败 53402 → 换封面立即重试）
                    mid, _cover_note = _add_draft_with_cover_retry(
                        title=article.title, body_html=body_html, author=job["author"],
                        digest=(article.body_md or "")[:100])
                    if _cover_note:
                        job["log"].append(f"  {_cover_note}")
                    job["log"].append(f"  第 {seq+1} 篇：草稿已建 media_id={mid[:16]}… 提交发布…")
                    # fast_fail：命中 53501 立即失败（实测限流恢复需 ~30 分钟，篇内长退避无意义且拖时间）
                    pub = wechat_api.publish_draft(mid, fast_fail=True)
                    pid = str(pub.get("publish_id") or "")
                    # 链接不在发布时逐篇查（freepublish/get 日配额极小，逐篇轮询会 45009 耗尽配额）；
                    # 统一在任务结束后用一次 batchget 批量回填（见 _backfill_urls）
                    url = ""
                    article.status = "published"; article.publish_id = pid; db.commit()
                    job["articles"][seq]["status"] = "published"
                    job["articles"][seq]["publish_id"] = pid
                    if url:
                        job["articles"][seq]["url"] = url
                    job["articles"][seq].pop("qr_url", None)
                    job["log"].append(f"✅ 第 {seq+1} 篇发布成功到 {job.get('account_name','该号')} (publish_id={pid})"
                                      + (f"\n   链接: {url}" if url else ""))
                except Exception as e:
                    logger.exception("api publish error")
                    article.status = "failed"
                    article.error = str(e)
                    db.commit()
                    job["articles"][seq]["status"] = "failed"
                    job["articles"][seq]["error"] = str(e)[:200]
                    if mid:
                        job["articles"][seq]["draft_mid"] = mid   # 保留草稿，便于稍后补发
                    job["articles"][seq].pop("qr_url", None)
                    job["log"].append(f"❌ 第 {seq+1} 篇 API 富排版发布失败: {e}")
                    if "53501" in str(e):
                        hit_limit = True
            else:
                # ---------- 扫码发布（原 playwright 流程） ----------
                cover = wx.pick_random_cover()
                qr_path = wx.get_current_qrcode_path(job_id, seq)
                log_path = LOG_DIR / f"{job_id}_{seq}.log"
                job["articles"][seq]["qr_url"] = f"/api/qr/{job_id}/{seq}"
                db.commit()

                result = wx.publish_one(
                    title=article.title,
                    body_md_path=md_path,
                    author=job["author"],
                    cover_path=cover,
                    qrcode_path=qr_path,
                    log_path=log_path,
                    publish_id_path=DATA_DIR / f"{job_id}_{seq}.pid",
                )

                if result["ok"]:
                    article.status = "published"
                    article.publish_id = result.get("publish_id", "")
                    article.cover_path = str(cover)
                    db.commit()
                    job["articles"][seq]["status"] = "published"
                    job["articles"][seq]["publish_id"] = result.get("publish_id", "")
                    job["log"].append(f"✅ 第 {seq+1} 篇发表成功 ({result.get('publish_id')})")
                elif result.get("draft_saved"):
                    # 扫码确认后微信只存了草稿、未真正发表（新版微信不稳定行为，勿当成功）
                    article.status = "failed"
                    article.error = "扫码后仅保存为草稿，未真正发表（文章在草稿箱，可改用 API 模式或对草稿箱用 API 补发）"
                    db.commit()
                    job["articles"][seq]["status"] = "failed"
                    job["articles"][seq]["error"] = article.error
                    job["log"].append(f"⚠️ 第 {seq+1} 篇：扫码后仅保存为草稿，未真正发表（文章在微信草稿箱）")
                else:
                    article.status = "failed"
                    article.error = result["log"][:500]
                    db.commit()
                    job["articles"][seq]["status"] = "failed"
                    job["articles"][seq]["error"] = result["log"][:200]
                    job["log"].append(f"❌ 第 {seq+1} 篇失败: {result['log'][:100]}")

            if hit_limit:
                # 命中微信频率限制：停止本轮剩余篇（避免继续连撞、加重冷却）；草稿已存微信草稿箱
                rest_n = max(job["count"] - seq - 1, 0)
                enq = []
                for rest in range(seq, job["count"]):
                    ai = job["articles"][rest] if rest < len(job["articles"]) else {}
                    art2 = db.query(Article).filter(Article.job_id == job_id, Article.seq == rest).first()
                    if rest > seq:
                        if art2:
                            art2.status = "skipped"
                            art2.error = "因微信频率限制(53501)未发布，已加入自动补发队列"
                            db.commit()
                        if rest < len(job["articles"]):
                            job["articles"][rest]["status"] = "skipped"
                            job["articles"][rest]["error"] = "因微信频率限制未发布（已加入自动补发队列）"
                    enq.append({
                        "topic": ai.get("topic") or "", "title": ai.get("title") or "",
                        "body_path": f"{job_id}_{rest}.md",        # 正文已在阶段1生成并落盘
                        "draft_mid": ai.get("draft_mid") or "",
                    })
                added = _plan_enqueue_items(enq)
                nxt_txt = datetime.fromtimestamp(int(_plan_load().get("next_ts") or 0)).strftime("%H:%M") if added else ""
                with jobs_lock:
                    job["log"].append(f"  ⚠️ 已停止剩余 {rest_n} 篇：微信限流(53501)。"
                                      + (f"已把 {added} 篇加入自动补发队列，{nxt_txt} 起自动重试（正文/草稿已保留，无需重新生成）" if added else "建议稍后再发"))
                if added and notify.configured():
                    try:
                        notify.send(f"⏳ 手动发布遇限流：已自动安排补发",
                                    f"本次有 {added} 篇未发出（微信限流 53501）。\n"
                                    f"系统已加入自动补发队列，{nxt_txt} 起自动重试，无需你操作。\n"
                                    f"也可改用「每日定时」自动分批发布。")
                    except Exception:
                        pass
                break

        with jobs_lock:
            job["phase"] = "cancelled" if job.get("cancel") else "done"
    except Exception as e:
        logger.exception("worker error")
        with jobs_lock:
            job["phase"] = "failed"
            job["log"].append(f"worker error: {e}")
    finally:
        db.close()
        # 任务结束后统一回填文章永久链接（用 batchget 批量取；逐篇查会耗尽 freepublish/get 日配额）
        _backfill_urls(job_id)
        # 操作日志：发布属于对外动作，必须留痕（含发起人）
        try:
            with jobs_lock:
                arts = list((jobs.get(job_id) or {}).get("articles") or [])
                op = dict((jobs.get(job_id) or {}).get("operator") or {})
            okn = sum(1 for a in arts if a.get("status") == "published")
            failn = sum(1 for a in arts if a.get("status") == "failed")
            audit.audit(None, "publish.done",
                        f"任务 {job_id} 结束：成功 {okn}/{len(arts)} 篇"
                        + (f"，失败 {failn} 篇" if failn else ""),
                        user={**op, "username": op.get("username") or "系统"} or None,
                        level="warn" if failn else "info")
        except Exception:
            pass
        # 任务结束（done/failed 均经此）→ 若该 job 标记需通知且已配置群机器人，则异步推送结果
        _launch_notify(job_id)


def _backfill_urls(job_id: str, rounds: int = 3, gap: int = 40):
    """任务发布结束后，回填每篇文章的永久链接（供微信通知里点击查看、网页展示）。

    ⚠️ 为什么不在发布时逐篇抓：freepublish/get 日配额极小——实测 19 篇 × 4 次轮询
       就把当天配额耗尽（45009 reach max api daily quota limit），导致后续大部分文章
       抓不到链接（表现为"只有开头几篇带链接"）。
       改用 freepublish/batchget：1 次调用最多取 20 篇，链接里 mid == publish_id，可精确匹配。
       刚提交的篇还在异步发布中（未进列表），故按 rounds 轮询等待（默认最多 3 轮）。
    """
    try:
        for r in range(max(1, rounds)):
            with jobs_lock:
                job = jobs.get(job_id)
                if not job:
                    return
                arts = job.get("articles", [])
                pending = [a for a in arts if a.get("publish_id") and not a.get("url")]
            if not pending:
                return
            m = wechat_api.fetch_recent_urls(40)
            filled = 0
            with jobs_lock:
                for a in arts:
                    if a.get("url") or not a.get("publish_id"):
                        continue
                    pid = str(a.get("publish_id"))
                    u = m["by_mid"].get(pid) or m["by_title"].get((a.get("title") or "").strip())
                    if u:
                        a["url"] = u
                        filled += 1
                if filled:
                    job.setdefault("log", []).append(f"🔗 已回填 {filled} 篇文章链接")
            # 同步到发布计划（批次路径的 items 也存 url，供补发/复用时带上）
            if m.get("by_title"):
                plan = _plan_load()
                changed = False
                for it in plan.get("items", []):
                    if it.get("url"):
                        continue
                    u = (m["by_mid"].get(str(it.get("publish_id") or ""))
                         or m["by_title"].get((it.get("title") or "").strip()))
                    if u:
                        it["url"] = u
                        changed = True
                if changed:
                    _plan_save(plan)
            with jobs_lock:
                job = jobs.get(job_id)
                still = [a for a in (job.get("articles", []) if job else [])
                         if a.get("publish_id") and not a.get("url")]
            if not still:
                return
            if r < max(1, rounds) - 1:
                time.sleep(gap)   # 等微信异步发布完成后再补一轮
    except Exception as e:  # noqa: BLE001
        logger.warning("回填文章链接异常：%s", str(e)[:120])


def _launch_notify(job_id: str):
    """任务完成后异步推送微信通知（仅当 job 标记 notify_on_done 且已配置群机器人）。

    ⚠️ 常见困惑（2026-10-15 用户反馈「手动发布没收到通知」）：手动发布是**勾选才发**——
       表单里的「📲 完成后微信通知」没勾就不发。所以这里在两种"没发"的情况下都往任务日志里写一行，
       让人在页面上能直接看到原因，而不是干等通知。
    """
    job = jobs.get(job_id)
    if not job:
        return
    if not job.get("notify_on_done"):
        with jobs_lock:
            job.setdefault("log", []).append(
                "ℹ️ 未勾选「📲 完成后微信通知」，本次不推送微信通知（下次勾上即可）")
        return
    if not notify.configured():
        # 定时任务默认要通知；未配置时给个一次性日志提示，不阻塞发布
        with jobs_lock:
            job.setdefault("log", []).append("⚠️ 任务需发送微信通知，但未配置 ROBOT_WEBHOOK_URL（企业微信群机器人），已跳过")
        return
    with jobs_lock:
        job.setdefault("log", []).append("📲 微信通知已在后台发送（含每篇标题与链接）")
    threading.Thread(target=lambda: notify.send_job_summary(job), daemon=True).start()


# ---- FastAPI app ----
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    _seed_defaults()
    _load_sched()   # 读取每日定时设置（默认每天 10:00）
    auth.ensure_admin()   # 首次启动创建管理员（用户名/密码见 README 或 ADMIN_INIT_PASSWORD）
    _migrate_account_owners()  # 旧库兼容：未归属公众号过户给管理员
    threading.Thread(target=_scheduler_loop, daemon=True, name="daily-scheduler").start()
    # 启动后 20 秒做一次卡片渲染自检（node 版本变化会导致渲染静默降级，2026-10-06 踩过）
    threading.Thread(target=lambda: (time.sleep(20), _ensure_card_render_ok("服务启动")),
                     daemon=True, name="render-selftest").start()
    yield


def _seed_defaults():
    """首次运行时，若库空则预置默认公众号 + 默认产品 + 默认主题，避免前端下拉/勾选为空。"""
    import wechat_api as _w
    db = SessionLocal()
    try:
        if db.query(Account).count() == 0:
            db.add(Account(name="百秘一梳服务号", appid=_w.get_account()[0],
                           secret_enc=_w.get_account()[1], author="百秘一梳"))
        if db.query(Product).count() == 0:
            db.add(Product(name="百秘一梳", style="green",
                           intro="天然桃木梳，取桃木为器、浸百草之液、藏养发精华，古法传承，源自汉末名医华佗一脉。"
                                 "理念：晨昏勤梳可养发养身、通经活络；天然材质、长辈相传、古法养生。"
                                 "软文中可作为天然养发/长辈礼物的自然背书，勿整段照搬典故。"))
        # 预置内置主题（只在空库时加一次；用户后续可在界面增删）
        if db.query(Topic).count() == 0:
            _builtin = [
                ("偏头痛", "偏头痛反复发作，用木梳头部按摩缓解"),
                ("失眠", "失眠难入睡，睡前梳头放松助眠"),
                ("脱发", "掉发/脱发/发际线后移，养发固发"),
                ("颈椎酸痛", "久坐低头颈椎僵硬，梳头通络缓解"),
                ("头皮屑痒", "头屑多、头皮痒，头皮清洁养护"),
                ("白头发早生", "年纪轻轻长白发，养发乌发"),
                ("干性发质", "头发干枯毛躁、分叉、打结"),
                ("睡眠浅易醒", "睡眠浅、多梦、易醒"),
                ("经常头晕", ""),
                ("肋痛", ""),
                ("乳房肿痛", ""),
                ("便秘", ""),
                ("高血压", ""),
                ("送礼", ""),
                ("领导送礼", ""),
                ("长辈送礼", ""),
                ("朋友送礼", ""),
                ("亲戚送礼", ""),
                ("同事送礼", ""),
                ("客户送礼", ""),
            ]
            for nm, ds in _builtin:
                db.add(Topic(name=nm, desc=ds))
        db.commit()
    finally:
        db.close()


app = FastAPI(title="润致公众号自动发布工具", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


# ============================================================
# 登录认证 / 用户管理 / 操作日志（2026-10-14：NAS + Cloudflare 公网访问）
# ============================================================
# 公开路径（其余一律要登录）：登录页与它需要的静态资源、健康检查
PUBLIC_PATHS = {"/login", "/api/auth/login", "/health",
                "/favicon.ico", "/static/logo.png", "/static/login.html"}


def _cookie_secure(request: Request) -> bool:
    """Cookie 是否加 Secure：auto=跟随请求协议（Cloudflare 隧道路径下自动开启）"""
    mode = (os.getenv("COOKIE_SECURE") or "auto").strip().lower()
    if mode == "always":
        return True
    if mode == "never":
        return False
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme or "").lower()
    return proto.startswith("https")


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """全站鉴权：未登录 → 页面跳 /login、接口 401；viewer 只读（写操作 403）"""
    path = request.url.path
    if path in PUBLIC_PATHS:
        return await call_next(request)
    u = auth.current_user(request)
    if not u:
        if path.startswith("/api/"):
            return JSONResponse({"detail": "未登录或登录已过期"}, status_code=401)
        from urllib.parse import quote
        return RedirectResponse(f"/login?next={quote(path)}", status_code=302)
    request.state.user = u
    if u["role"] == "viewer" and request.method not in ("GET", "HEAD", "OPTIONS"):
        if not path.startswith("/api/auth/"):
            return JSONResponse({"detail": "只读账号无权执行此操作"}, status_code=403)
    return await call_next(request)


@app.get("/health")
async def health():
    """健康检查（公开）：Docker/Cloudflare 探活用"""
    return {"ok": True, "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}


@app.get("/login")
async def login_page(request: Request):
    if auth.current_user(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(str(APP_DIR / "static" / "login.html"))


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/auth/login")
async def api_login(body: LoginIn, request: Request):
    ip = auth.client_ip(request)
    un = (body.username or "").strip()
    if not auth.login_allowed(ip, un):
        audit.audit(request, "auth.login_fail", f"登录过于频繁已临时锁定（用户 {un}）",
                    result="fail", level="warn")
        raise HTTPException(429, "登录失败次数过多，请 5 分钟后再试")
    u = auth.authenticate(un, body.password)
    if not u:
        auth.mark_login_fail(ip, un)
        audit.audit(request, "auth.login_fail", f"用户名或密码错误（用户 {un}）",
                    result="fail", level="warn")
        raise HTTPException(401, "用户名或密码错误")
    auth.clear_login_fail(ip, un)
    token = auth.make_session_token(u["id"], u["username"], u["role"])
    resp = JSONResponse({"ok": True, "user": u})
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    secure=_cookie_secure(request), path="/",
                    max_age=auth.SESSION_TTL_HOURS * 3600)
    audit.audit(request, "auth.login", f"登录成功（{u['username']} / {auth.ROLES.get(u['role'])}）", user=u)
    logger.info("用户登录：%s from %s", u["username"], ip)
    return resp


@app.post("/api/auth/logout")
async def api_logout(request: Request):
    u = auth.current_user(request)
    if u:
        audit.audit(request, "auth.logout", f"退出登录（{u['username']}）", user=u)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.get("/api/auth/me")
async def api_me(request: Request):
    u = auth.current_user(request)
    if not u:
        raise HTTPException(401, "未登录")
    return u


class PwIn(BaseModel):
    old_password: str
    new_password: str


@app.post("/api/auth/password")
async def api_change_pw(body: PwIn, request: Request):
    u = auth.need_login(request)
    ok, msg = auth.change_password(u["id"], body.old_password, body.new_password)
    audit.audit(request, "auth.password", f"修改密码：{msg}", user=u,
                result="ok" if ok else "fail", level="info" if ok else "warn")
    if not ok:
        raise HTTPException(400, msg)
    return {"ok": True, "msg": msg}


# ---------- 用户管理（仅管理员） ----------
class UserIn(BaseModel):
    username: str = ""
    password: str = ""
    name: str = ""
    role: str = "editor"
    is_active: int = 1
    # 管理员授权：允许该用户使用共享模型/Key（None = 本次不改动）。普通用户自己改不了这个字段。
    llm_use_shared: Optional[bool] = None


def _user_llm_public(u, db) -> dict:
    """给「用户管理」列出某个用户的模型配置概况（**绝不回显 Key 明文**，只说来源与状态）。"""
    pub = auth.public_user(u)
    s = db_api.get_user_setting(u.id, db)
    use_shared = bool(s and s.llm_use_shared)
    cfg, err = _resolve_llm_for_user(u.id, is_admin=(u.role == "admin"), db=db)
    if err:
        status = "missing"
    elif cfg["key_source"] == "你自己的 Key":
        status = "own"
    elif cfg["using_shared"] and cfg["api_key"]:
        status = "shared"
    else:
        status = "not_required"
    pub.update({
        "llm_use_shared": use_shared,
        "llm_provider": (s.llm_provider if s else "") or "",
        "llm_model": (s.llm_model if s else "") or "",
        "has_own_key": bool(s and (s.llm_api_key or "").strip()),
        "key_status": status,            # own / shared / missing / not_required
        "key_source": cfg["key_source"],
        "is_admin_user": u.role == "admin",
    })
    return pub


@app.get("/api/users")
async def api_users(request: Request):
    auth.need_role(request, "admin")
    db = SessionLocal()
    try:
        return [_user_llm_public(u, db) for u in db.query(User).order_by(User.id).all()]
    finally:
        db.close()


@app.post("/api/users")
async def api_user_add(body: UserIn, request: Request):
    me = auth.need_role(request, "admin")
    un = (body.username or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,32}", un):
        raise HTTPException(400, "用户名只能是 3-32 位字母/数字/._@-")
    if len(body.password or "") < 8:
        raise HTTPException(400, "密码至少 8 位")
    if body.role not in auth.ROLES:
        raise HTTPException(400, "角色不合法")
    db = SessionLocal()
    try:
        if db.query(User).filter(User.username == un).first():
            raise HTTPException(400, "用户名已存在")
        u = User(username=un, name=(body.name or "").strip() or un, role=body.role,
                 password_hash=auth.hash_password(body.password), is_active=1 if body.is_active else 0)
        db.add(u); db.commit(); db.refresh(u)
        if body.llm_use_shared is not None:
            db_api.upsert_user_setting(u.id, db=db, llm_use_shared=1 if body.llm_use_shared else 0)
        audit.audit(request, "user.create",
                    f"新增用户 {u.username}（{u.name}，角色 {auth.ROLES.get(u.role)}，"
                    f"{'启用' if u.is_active else '停用'}"
                    + ("，已授权共享模型" if body.llm_use_shared else "") + ")", user=me)
        return {"ok": True, "user": _user_llm_public(u, db)}
    finally:
        db.close()


@app.put("/api/users/{uid}")
async def api_user_update(uid: int, body: UserIn, request: Request):
    me = auth.need_role(request, "admin")
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(404, "用户不存在")
        changes = []
        if body.name and body.name.strip() != (u.name or ""):
            changes.append(f"姓名 {u.name}→{body.name.strip()}"); u.name = body.name.strip()
        if body.role and body.role != u.role:
            if body.role not in auth.ROLES:
                raise HTTPException(400, "角色不合法")
            if u.role == "admin" and body.role != "admin" and _count_active_admins(db, exclude=u.id) == 0:
                raise HTTPException(400, "至少要保留一名启用状态的管理员")
            changes.append(f"角色 {auth.ROLES.get(u.role)}→{auth.ROLES.get(body.role)}"); u.role = body.role
        if body.is_active is not None and int(body.is_active) != int(u.is_active):
            if int(body.is_active) == 0:
                if u.id == me["id"]:
                    raise HTTPException(400, "不能停用当前登录的账号")
                if u.role == "admin" and _count_active_admins(db, exclude=u.id) == 0:
                    raise HTTPException(400, "至少要保留一名启用状态的管理员")
            changes.append("停用" if not body.is_active else "启用"); u.is_active = 1 if body.is_active else 0
        if body.password:
            if len(body.password) < 8:
                raise HTTPException(400, "密码至少 8 位")
            u.password_hash = auth.hash_password(body.password)
            u.must_change_pw = 0
            changes.append("重置密码")
        # 管理员授权/取消授权：允许该用户使用共享模型（含共享 Key）。不传则不动。
        if body.llm_use_shared is not None:
            _us = db_api.get_user_setting(uid, db)
            _cur = bool(_us and _us.llm_use_shared)
            if bool(body.llm_use_shared) != _cur:
                db_api.upsert_user_setting(uid, db=db,
                                           llm_use_shared=1 if body.llm_use_shared else 0)
                changes.append("授权共享模型" if body.llm_use_shared else "取消共享模型授权")
        db.commit()
        audit.audit(request, "user.update", f"修改用户 {u.username}：{'；'.join(changes) or '无变化'}",
                    user=me, level="warn" if "重置密码" in changes or "停用" in changes else "info")
        return {"ok": True, "user": _user_llm_public(u, db)}
    finally:
        db.close()


@app.delete("/api/users/{uid}")
async def api_user_delete(uid: int, request: Request):
    me = auth.need_role(request, "admin")
    db = SessionLocal()
    try:
        u = db.query(User).filter(User.id == uid).first()
        if not u:
            raise HTTPException(404, "用户不存在")
        if u.id == me["id"]:
            raise HTTPException(400, "不能删除当前登录的账号")
        if u.role == "admin" and _count_active_admins(db, exclude=u.id) == 0:
            raise HTTPException(400, "至少要保留一名启用状态的管理员")
        un, role = u.username, u.role
        db.delete(u); db.commit()
        audit.audit(request, "user.delete", f"删除用户 {un}（{auth.ROLES.get(role)}）",
                    user=me, level="warn")
        return {"ok": True}
    finally:
        db.close()


def _count_active_admins(db, exclude: int = 0) -> int:
    q = db.query(User).filter(User.role == "admin", User.is_active == 1)
    if exclude:
        q = q.filter(User.id != exclude)
    return q.count()


# ---------- 操作日志（仅管理员） ----------
@app.get("/api/audit")
async def api_audit(request: Request, limit: int = 100, offset: int = 0, q: str = "",
                    action: str = "", username: str = "", level: str = ""):
    auth.need_role(request, "admin")
    return audit.query_logs(limit=limit, offset=offset, q=q, action=action,
                            username=username, level=level)


@app.get("/api/audit/export.csv")
async def api_audit_export(request: Request, limit: int = 5000):
    me = auth.need_role(request, "admin")
    audit.audit(request, "audit.export", f"导出操作日志（{limit} 条上限）", user=me)
    return PlainTextResponse(audit.export_csv(limit=limit), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="audit_logs.csv"'})


class RunRequest(BaseModel):
    # 多主题：topics=[{"name":"偏头痛","count":2},{"name":"失眠","count":1}]，每主题独立生成 count 篇。
    # 兼容旧参数 topic（单主题字符串）：若 topics 为空则按 [{"name": topic, "count": count}] 处理。
    topics: Optional[list] = None
    topic: str = ""
    count: int = 3
    brand: str = "百秘一梳"        # 兼容旧参数（产品未选时兜底用）
    author: str = "百秘一梳"
    extra_hint: str = ""
    # 发布方式：use_api=True → 官方 API 全自动发布；False → 扫码发布
    use_api: bool = False
    # 多产品/多公众号/作者（新）
    account_id: int = 0            # 目标公众号 Account.id；0=用默认/硬编码
    product_id: int = 0            # 所选产品 Product.id；0=用 brand 兜底
    # LLM provider
    provider: str = "deepseek"
    api_key: str = ""
    base_url: str = ""
    model: str = ""
    # 完成通知：True 则任务结束后推送到企业微信群机器人（需配 ROBOT_WEBHOOK_URL）
    notify: bool = False


class AccountIn(BaseModel):
    name: str
    appid: str
    secret: str
    author: str = "百秘一梳"


class ProductIn(BaseModel):
    name: str
    intro: str = ""
    style: str = ""


# ========== 产品库 CRUD ==========
# 归属规则（2026-10-16）：
#   · owner_id = NULL → 公共素材：所有人可见；**只有管理员能改删**（防误改公共内容）
#   · owner_id = 某人 → 私有：仅本人 + 管理员可见可改删
#   · 管理员新建 → 自动落 NULL（作为团队公共素材）；普通用户新建 → 归自己
@app.get("/api/products")
async def list_products_api(request: Request):
    """产品列表：管理员看全部；其余用户只看【自己创建的 + 公共(NULL)】。
    返回 mine / shared / can_edit 供前端决定是否显示「编辑 / 删除」。"""
    me = auth.need_login(request)
    is_admin = me["role"] == "admin"
    db = SessionLocal()
    try:
        q = db.query(Product).order_by(Product.id)
        cond = db_api.owner_scope_filter(Product, me["id"], is_admin)
        if cond is not None:
            q = q.filter(cond)
        return [{"id": p.id, "name": p.name, "intro": p.intro, "style": p.style,
                 "owner_id": p.owner_id,
                 "mine": (p.owner_id == me["id"]),
                 "shared": (p.owner_id is None),
                 "can_edit": db_api.can_edit_owned(p, me["id"], is_admin)}
                for p in q.all()]
    finally:
        db.close()


@app.post("/api/products")
async def add_product_api(p: ProductIn, request: Request):
    me = auth.need_role(request, "editor")
    if not p.name.strip():
        raise HTTPException(400, "产品名不能为空")
    is_admin = me["role"] == "admin"
    db = SessionLocal()
    try:
        # 管理员新建 → 公共素材（全团队可用）；普通用户新建 → 归自己
        prod = Product(name=p.name.strip(), intro=p.intro.strip(), style=p.style.strip(),
                       owner_id=(None if is_admin else me["id"]))
        db.add(prod); db.commit(); db.refresh(prod)
        audit.audit(request, "product.create",
                    f"新增产品「{prod.name}」（{'公共' if prod.owner_id is None else '私有'}）", user=me)
        return {"id": prod.id, "name": prod.name}
    finally:
        db.close()


@app.put("/api/products/{pid}")
async def edit_product_api(pid: int, p: ProductIn, request: Request):
    me = auth.need_role(request, "editor")
    is_admin = me["role"] == "admin"
    if not p.name.strip():
        raise HTTPException(400, "产品名不能为空")
    db = SessionLocal()
    try:
        prod = db.query(Product).filter(Product.id == pid).first()
        if not prod:
            raise HTTPException(404, "产品不存在")
        if not db_api.can_edit_owned(prod, me["id"], is_admin):
            raise HTTPException(403, "该产品不是你创建的，无权修改（公共产品请让管理员改）")
        prod.name = p.name.strip()
        prod.intro = p.intro.strip()
        prod.style = p.style.strip()
        db.commit()
        audit.audit(request, "product.update", f"修改产品「{prod.name}」(#{prod.id})", user=me)
        return {"ok": True, "id": prod.id, "name": prod.name}
    finally:
        db.close()


@app.delete("/api/products/{pid}")
async def del_product_api(pid: int, request: Request):
    me = auth.need_role(request, "editor")
    is_admin = me["role"] == "admin"
    db = SessionLocal()
    try:
        prod = db.query(Product).filter(Product.id == pid).first()
        if prod:
            if not db_api.can_edit_owned(prod, me["id"], is_admin):
                raise HTTPException(403, "该产品不是你创建的，无权删除（公共产品请让管理员删）")
            nm = prod.name
            db.delete(prod); db.commit()
            audit.audit(request, "product.delete", f"删除产品「{nm}」", user=me, level="warn")
        return {"ok": True}
    finally:
        db.close()


# ========== 主题库 CRUD ==========
def _product_name(db, pid: int) -> str:
    """产品 id → 名称（仅用于日志/审计文案）；0 或找不到 → 「通用（所有产品）」。"""
    if not pid:
        return "通用（所有产品）"
    p = db.query(Product).filter(Product.id == pid).first()
    return p.name if p else f"#{pid}（已删除）"


@app.get("/api/topics")
async def list_topics_api(request: Request, product_id: int = -1):
    """主题列表。product_id ≥ 0 时只返回【该产品专属 + 通用(0)】的主题
    —— 前端选产品后据此过滤，避免美食产品看到保健主题；不传则返回全部（主题库管理页用）。
    可见性：管理员看全部；其余用户只看【自己创建的 + 公共(NULL)】。"""
    me = auth.need_login(request)
    is_admin = me["role"] == "admin"
    db = SessionLocal()
    try:
        q = db.query(Topic).order_by(Topic.id)
        if product_id >= 0:
            q = q.filter((Topic.product_id == product_id) | (Topic.product_id == 0))
        cond = db_api.owner_scope_filter(Topic, me["id"], is_admin)
        if cond is not None:
            q = q.filter(cond)
        return [{"id": t.id, "name": t.name, "desc": t.desc,
                 "product_id": (t.product_id or 0),
                 "owner_id": t.owner_id,
                 "mine": (t.owner_id == me["id"]),
                 "shared": (t.owner_id is None),
                 "can_edit": db_api.can_edit_owned(t, me["id"], is_admin)}
                for t in q.all()]
    finally:
        db.close()


class TopicIn(BaseModel):
    name: str
    desc: str = ""
    product_id: int = 0     # 归属产品：0=通用（所有产品可见）


@app.post("/api/topics")
async def add_topic_api(t: TopicIn, request: Request):
    me = auth.need_role(request, "editor")
    is_admin = me["role"] == "admin"
    if not t.name.strip():
        raise HTTPException(400, "主题名不能为空")
    db = SessionLocal()
    try:
        # 重名校验只在【自己可见范围】内做：不同用户的私有主题允许同名
        dup = db.query(Topic).filter(Topic.name == t.name.strip())
        cond = db_api.owner_scope_filter(Topic, me["id"], is_admin)
        if cond is not None:
            dup = dup.filter(cond)
        if dup.first():
            raise HTTPException(400, "主题已存在（你自己或公共主题里已有同名）")
        pname = _product_name(db, int(t.product_id or 0))
        # 管理员新建 → 公共主题（全团队可用）；普通用户新建 → 归自己
        tp = Topic(name=t.name.strip(), desc=t.desc.strip(), product_id=int(t.product_id or 0),
                   owner_id=(None if is_admin else me["id"]))
        db.add(tp); db.commit(); db.refresh(tp)
        audit.audit(request, "topic.create",
                    f"新增主题「{tp.name}」（归属产品：{pname}；"
                    f"{'公共' if tp.owner_id is None else '私有'}）", user=me)
        return {"id": tp.id, "name": tp.name, "product_id": tp.product_id or 0,
                "owner_id": tp.owner_id}
    finally:
        db.close()


@app.put("/api/topics/{tid}")
async def edit_topic_api(tid: int, t: TopicIn, request: Request):
    me = auth.need_role(request, "editor")
    is_admin = me["role"] == "admin"
    if not t.name.strip():
        raise HTTPException(400, "主题名不能为空")
    db = SessionLocal()
    try:
        tp = db.query(Topic).filter(Topic.id == tid).first()
        if not tp:
            raise HTTPException(404, "主题不存在")
        if not db_api.can_edit_owned(tp, me["id"], is_admin):
            raise HTTPException(403, "该主题不是你创建的，无权修改（公共主题请让管理员改）")
        dup = db.query(Topic).filter(Topic.name == t.name.strip(), Topic.id != tid)
        cond = db_api.owner_scope_filter(Topic, me["id"], is_admin)
        if cond is not None:
            dup = dup.filter(cond)
        if dup.first():
            raise HTTPException(400, "已有同名主题")
        tp.name = t.name.strip()
        tp.desc = t.desc.strip()
        tp.product_id = int(t.product_id or 0)
        db.commit()
        audit.audit(request, "topic.update",
                    f"修改主题「{tp.name}」（归属：{_product_name(db, tp.product_id)}）", user=me)
        return {"ok": True, "id": tp.id, "name": tp.name, "product_id": tp.product_id or 0}
    finally:
        db.close()


class TopicBulkAssignIn(BaseModel):
    ids: list = []              # 要归类的主题 id 列表
    product_id: int = 0         # 目标归属；0 = 设为通用


@app.post("/api/topics/bulk-assign")
async def bulk_assign_topics(body: TopicBulkAssignIn, request: Request):
    """批量把多条主题归到同一个产品（product_id=0 → 设为通用）。
    主题库管理页的「批量归类」用它，避免逐条 PUT 发 N 个请求。"""
    me = auth.need_role(request, "editor")
    is_admin = me["role"] == "admin"
    try:
        ids = [int(x) for x in (body.ids or [])]
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "ids 必须是数字列表")
    if not ids:
        raise HTTPException(400, "请先勾选要归类的主题")
    pid = int(body.product_id or 0)
    db = SessionLocal()
    try:
        rows = db.query(Topic).filter(Topic.id.in_(ids)).all()
        if not rows:
            raise HTTPException(404, "未找到这些主题")
        # 只归类有权限的：非管理员只能动自己创建的主题（公共/他人的会被跳过）
        allowed = [t for t in rows if db_api.can_edit_owned(t, me["id"], is_admin)]
        if not allowed:
            raise HTTPException(403, "所选主题都不是你创建的，无权归类（公共主题请让管理员改）")
        for t in allowed:
            t.product_id = pid
        db.commit()
        n = len(allowed)
        skipped = len(rows) - n
        pname = _product_name(db, pid)
        audit.audit(request, "topic.bulk_assign",
                    f"批量归类 {n} 个主题 → 「{pname}」"
                    + (f"（跳过无权限 {skipped} 个）" if skipped else "")
                    + f"（#{','.join(str(t.id) for t in allowed)[:80]}）",
                    user=me, level="warn")
        return {"ok": True, "updated": n, "skipped": skipped, "product_id": pid}
    finally:
        db.close()


@app.delete("/api/topics/{tid}")
async def del_topic_api(tid: int, request: Request):
    me = auth.need_role(request, "editor")
    is_admin = me["role"] == "admin"
    db = SessionLocal()
    try:
        tp = db.query(Topic).filter(Topic.id == tid).first()
        if tp:
            if not db_api.can_edit_owned(tp, me["id"], is_admin):
                raise HTTPException(403, "该主题不是你创建的，无权删除（公共主题请让管理员删）")
            nm = tp.name
            db.delete(tp); db.commit()
            audit.audit(request, "topic.delete", f"删除主题「{nm}」", user=me, level="warn")
        return {"ok": True}
    finally:
        db.close()


# ========== 公众号账号 CRUD ==========
@app.get("/api/accounts")
async def list_accounts_api(request: Request):
    """账号列表：仅返回当前用户可见的公众号（自己的 + 系统共享NULL）；AppSecret 只对管理员返回"""
    u = auth.need_login(request)
    is_admin = u["role"] == "admin"
    db = SessionLocal()
    try:
        out = []
        for a in db_api.list_accounts_for_user(u["id"], is_admin, db):
            sec = a.secret_enc or ""
            out.append({"id": a.id, "name": a.name, "appid": a.appid,
                        "secret": sec if is_admin else (sec[:4] + "****" if sec else ""),
                        "secret_masked": not is_admin, "author": a.author,
                        "owner_id": a.owner_id, "mine": (a.owner_id == u["id"])})
        return out
    finally:
        db.close()


@app.post("/api/accounts")
async def add_account_api(a: AccountIn, request: Request):
    me = auth.need_role(request, "editor")    # 每个登录用户都能添加自己的公众号
    if not a.name.strip() or not a.appid.strip() or not a.secret.strip():
        raise HTTPException(400, "账号名/appid/secret 不能为空")
    db = SessionLocal()
    try:
        exists = db.query(Account).filter(Account.appid == a.appid.strip()).first()
        if exists:
            raise HTTPException(400, f"已存在 appid={a.appid.strip()} 的公众号")
        acct = Account(name=a.name.strip(), appid=a.appid.strip(),
                       secret_enc=a.secret.strip(), author=a.author.strip() or "百秘一梳",
                       owner_id=me["id"])
        db.add(acct); db.commit(); db.refresh(acct)
        audit.audit(request, "account.create", f"新增公众号「{acct.name}」(appid={acct.appid})",
                    user=me, level="warn")
        return {"id": acct.id, "name": acct.name}
    finally:
        db.close()


@app.delete("/api/accounts/{aid}")
async def del_account_api(aid: int, request: Request):
    me = auth.need_login(request)
    is_admin = me["role"] == "admin"
    db = SessionLocal()
    try:
        acct = db.query(Account).filter(Account.id == aid).first()
        if not acct:
            return {"ok": True}
        if not is_admin and acct.owner_id != me["id"]:
            raise HTTPException(403, "只能删除你自己的公众号")
        nm = acct.name
        db.delete(acct); db.commit()
        audit.audit(request, "account.delete", f"删除公众号「{nm}」", user=me, level="warn")
        return {"ok": True}
    finally:
        db.close()


@app.get("/api/providers")
async def api_providers():
    """返回可选 LLM provider 列表（含默认值）供前端下拉框使用"""
    return llm.list_providers()


# ========== 每用户私有设置（多租户：自己的模型/Key + 自己的定时） ==========
class UserSettingsIn(BaseModel):
    llm_provider: str = "deepseek"
    llm_base_url: str = ""
    llm_model: str = ""
    # 该用户自己的 Key。⚠️ 留空**不等于**可以用服务端默认 Key —— 只有管理员授权(use_shared)才允许（2026-10-16）
    llm_api_key: str = ""
    default_account_id: int = 0
    schedule_enabled: bool = False
    schedule_time: str = "10:00"
    schedule_account_ids: list = []   # 勾选定时发布的自有公众号 id 列表
    schedule_product_id: int = 0


@app.get("/api/user/settings")
async def api_user_settings_get(request: Request):
    """返回当前登录用户的私有设置（模型/Key + 定时）。Key 仅本人可见。
    另附 key_status / key_hint：让前端明确显示「你在用谁的 Key」，避免误以为能白用默认 Key。"""
    me = auth.need_login(request)
    s = db_api.get_user_setting(me["id"])
    use_shared = bool(s and s.llm_use_shared)
    is_admin = me["role"] == "admin"
    cfg, err = _resolve_llm_for_user(me["id"])
    if err:
        key_status, key_hint = "missing", err
    elif cfg["key_source"] == "你自己的 Key":
        key_status, key_hint = "own", "当前使用：你自己填写的 API Key（各人消耗各自的额度）"
    elif cfg["using_shared"] and cfg["api_key"]:
        key_status = "shared"
        if is_admin:
            key_hint = ("当前使用：服务端默认 Key（你是管理员，即该 Key 的所有者；"
                        "要在上面填自己的 Key 也可以，填了就优先用你自己的）")
        else:
            key_hint = ("当前使用：管理员共享的模型/Key（由管理员授权，"
                        + ("取自管理员的设置" if "服务端默认" not in cfg["key_source"] else "取自服务端默认配置") + "）")
    else:
        key_status, key_hint = "not_required", "当前模型为本地/自建服务，通常不需要 API Key"
    return {
        "llm_provider": s.llm_provider if s else "deepseek",
        "llm_base_url": s.llm_base_url if s else "",
        "llm_model": s.llm_model if s else "",
        "llm_api_key": s.llm_api_key if s else "",
        "default_account_id": s.default_account_id if s else 0,
        "schedule_enabled": bool(s.schedule_enabled) if s else False,
        "schedule_time": s.schedule_time if s else "10:00",
        "schedule_account_ids": (json.loads(s.schedule_account_ids) if s and s.schedule_account_ids else []) if s else [],
        "schedule_product_id": s.schedule_product_id if s else 0,
        # 新增：Key 归属/授权状态（前端据此提示）
        "use_shared": use_shared,
        "can_use_shared": is_admin,        # 管理员本身即 Key 所有者，无需"授权"
        "key_status": key_status,          # own / shared / missing / not_required
        "key_hint": key_hint,
        "key_source": cfg["key_source"],
        "requires_key": cfg["requires_key"],
        "provider": cfg["provider"],
    }


@app.post("/api/user/settings")
async def api_user_settings_set(body: UserSettingsIn, request: Request):
    me = auth.need_login(request)
    is_admin = me["role"] == "admin"
    if body.llm_provider not in llm.PROVIDERS:
        raise HTTPException(400, f"provider 必须是 {list(llm.PROVIDERS)} 之一")
    t = (body.schedule_time or "").strip()
    if t and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", t):
        raise HTTPException(400, "定时时间格式应为 HH:MM，如 10:00")
    # 校验勾选的公众号都属于该用户（或系统共享）
    try:
        aids = [int(x) for x in (body.schedule_account_ids or [])]
    except Exception:
        raise HTTPException(400, "schedule_account_ids 必须是数字列表")
    db = SessionLocal()
    try:
        owned = {a.id for a in db_api.list_accounts_for_user(me["id"], is_admin, db)}
        bad = [x for x in aids if x not in owned]
        if bad:
            raise HTTPException(400, f"以下公众号不属于你或无权限：{bad}")
        # ⚠️ llm_use_shared 只能由管理员在「用户管理」里授予，用户自己保存设置时**不允许**改动它
        db_api.upsert_user_setting(
            me["id"], db=db,
            llm_provider=body.llm_provider, llm_base_url=(body.llm_base_url or "").strip(),
            llm_model=(body.llm_model or "").strip(), llm_api_key=body.llm_api_key or "",
            default_account_id=int(body.default_account_id or 0),
            schedule_enabled=1 if body.schedule_enabled else 0,
            schedule_time=t or "10:00",
            schedule_account_ids=json.dumps(aids, ensure_ascii=False),
            schedule_product_id=int(body.schedule_product_id or 0),
        )
    finally:
        db.close()
    # 保存后立刻评估「这套配置能不能真的跑」——不能跑就给前端一条明确警告（发布时同样会被拦）
    _cfg, _err = _resolve_llm_for_user(me["id"])
    audit.audit(request, "settings.user",
                f"更新个人设置（模型/Key/定时）：{body.llm_provider}"
                + ("；⚠️ 缺 Key，发布将被拦" if _err else ""), user=me,
                level="warn" if _err else "info")
    return {"ok": True, "warning": _err or "", "key_status": ("missing" if _err else "ok"),
            "key_source": _cfg["key_source"], "requires_key": _cfg["requires_key"]}


class LlmTestIn(BaseModel):
    """「测试连通」入参：用当前表单里的配置测；留空的字段回退到该用户已保存的设置。"""
    provider: str = ""
    base_url: str = ""
    model: str = ""
    api_key: str = ""


@app.post("/api/llm/test")
async def api_llm_test(body: LlmTestIn, request: Request):
    """测试大模型连通性（同步、短超时，供设置页按钮即时反馈）。

    · 只做一次最小请求（max_tokens=8 的 "ping"），不会真正生成文章、不计费多少；
    · 未填写的字段按「表单 → 该用户已保存 → 服务端默认」优先级解析，
      与真正生成时用的配置一致（并在返回里告知 Key 的实际来源）；
    · ⚠️ 与发布走**同一套多租户 Key 策略**：未获管理员授权又没填自己的 Key → 直接给明确原因，
      不会偷偷拿服务端默认 Key 去测（否则用户会以为"能测通就能发"）；
    · 返回 ok / 耗时 / 实际模型与地址 / 可读错误提示，方便用户当场定位问题。
    """
    me = auth.need_login(request)
    saved = db_api.get_user_setting(me["id"])
    prov = (body.provider or (saved.llm_provider if saved else "") or "deepseek").strip()
    if prov not in llm.PROVIDERS:
        raise HTTPException(400, f"provider 必须是 {list(llm.PROVIDERS)} 之一")
    base_url = (body.base_url or (saved.llm_base_url if saved else "") or "").strip()
    model = (body.model or (saved.llm_model if saved else "") or "").strip()
    api_key = (body.api_key or (saved.llm_api_key if saved else "") or "").strip()

    # 先用同一套策略解析（显式传入的 key 视为"他自己的"，可直接测）
    cfg, perr = _resolve_llm_for_user(me["id"], provider=prov, base_url=base_url,
                                      model=model, api_key=api_key)
    info = llm.describe_config(cfg["provider"], api_key=cfg["api_key"], base_url=cfg["base_url"],
                               model=cfg["model"], key_origin=cfg["key_source"],
                               allow_env_key=cfg["allow_env_key"])
    if perr:
        return {"ok": False, "elapsed_ms": 0, "hint": perr, "error": "",
                "blocked": True, **info}
    prov, api_key, base_url, model = cfg["provider"], cfg["api_key"], cfg["base_url"], cfg["model"]

    t0 = time.time()
    try:
        ok, err = llm.ping(prov, api_key=api_key, base_url=base_url, model=model, timeout=20,
                           allow_env_key=cfg["allow_env_key"])
    except Exception as e:  # noqa: BLE001 —— ping 内部已兜异常，这里只防御未知情况
        ok, err = False, str(e)[:200]
    ms = int((time.time() - t0) * 1000)
    if ok:
        hint = "连通正常，可以正常生成文章"
        if info["is_local"]:
            hint = "连通正常（本地/自建模型为逐篇生成，速度取决于你的机器）"
    else:
        hint = llm.describe_error(err)
        if _is_llm_auth_error(err):
            hint = "鉴权失败：API Key 无效或无权限" + (f"（当前使用的是「{info['key_source']}」的 Key）"
                                                        if info.get("key_source") else "")
    audit.audit(request, "llm.test",
                f"测试大模型连通：{prov} · {info['model']} → {'成功' if ok else '失败'}（{ms}ms）",
                user=me, level="info" if ok else "warn")
    return {"ok": bool(ok), "elapsed_ms": ms, "error": (err or "")[:300], "hint": hint, **info}


class AdminUserScheduleIn(BaseModel):
    """管理员代管某用户每日定时的入参（只管定时，不动其模型/Key）。"""
    schedule_enabled: bool = False
    schedule_time: str = "10:00"
    schedule_account_ids: list = []
    schedule_product_id: int = 0


@app.get("/api/admin/user-schedules")
async def api_admin_user_schedules(request: Request):
    """管理员：查看所有用户的每日定时（含每人可勾选的公众号范围、公众号/产品列表、全局兜底定时）。
    前端据此渲染「🗓 定时总览」面板：一眼看全谁的定时开着、几点、发哪几个号、用哪款产品。"""
    auth.need_role(request, "admin")
    db = SessionLocal()
    try:
        users = db.query(User).order_by(User.id).all()
        accounts = db.query(Account).order_by(Account.id).all()
        products = db_api.list_products(db)
        acct_name = {a.id: a.name for a in accounts}
        out = []
        for u in users:
            s = db_api.get_user_setting(u.id, db)
            aids = []
            if s and s.schedule_account_ids:
                try:
                    aids = [int(x) for x in json.loads(s.schedule_account_ids)]
                except Exception:  # noqa: BLE001
                    aids = []
            # 该用户可被授权使用的号：自己创建的 + 系统共享(owner_id IS NULL)
            allowed = sorted(a.id for a in accounts if a.owner_id is None or a.owner_id == u.id)
            out.append({
                "id": u.id,
                "username": u.username,
                "name": u.name or "",
                "role": u.role,
                "is_active": bool(getattr(u, "is_active", 1)),
                "schedule_enabled": bool(s.schedule_enabled) if s else False,
                "schedule_time": (s.schedule_time if s else "") or "10:00",
                "schedule_account_ids": aids,
                "schedule_product_id": (s.schedule_product_id if s else 0) or 0,
                "schedule_last_run_date": (s.schedule_last_run_date if s else "") or "",
                "allowed_account_ids": allowed,
                "account_names": [acct_name.get(x, f"#{x}") for x in aids],
                "product_name": next((p.name for p in products
                                      if p.id == ((s.schedule_product_id if s else 0) or 0)), ""),
                "llm_provider": (s.llm_provider if s else "") or "",
                "llm_model": (s.llm_model if s else "") or "",
                "has_own_key": bool(s.llm_api_key) if s else False,
                # Key 策略：能否用共享模型/Key + 当前状态（定时总览里一眼看出谁没 Key 会发不出）
                "llm_use_shared": bool(s.llm_use_shared) if s else False,
                "key_status": _user_llm_public(u, db)["key_status"],
            })
        return {
            "users": out,
            "accounts": [{"id": a.id, "name": a.name, "owner_id": a.owner_id} for a in accounts],
            "products": [{"id": p.id, "name": p.name} for p in products],
            # 全局兜底定时：仅当【无人启用个人定时】时才生效（向后兼容单人部署）
            "global_fallback": {
                "enabled": bool(SCHED_CFG.get("enabled")),
                "time": SCHED_CFG.get("time", "10:00"),
                "account_id": SCHED_CFG.get("account_id", 0),
                "product_id": SCHED_CFG.get("product_id", 0),
                "last_run_date": SCHED_CFG.get("last_run_date", ""),
            },
            "multi_user": len(users) > 1,
        }
    finally:
        db.close()


@app.post("/api/admin/user-schedules/{uid}")
async def api_admin_set_user_schedule(uid: int, body: AdminUserScheduleIn, request: Request):
    """管理员代管：修改指定用户的每日定时。
    ⚠️ 只能选【该用户自己创建的 + 系统共享】的公众号 —— 越权分配会被拒绝（保持多租户隔离不变）。"""
    me = auth.need_role(request, "admin")
    t = (body.schedule_time or "").strip()
    if t and not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", t):
        raise HTTPException(400, "定时时间格式应为 HH:MM，如 10:00")
    try:
        aids = [int(x) for x in (body.schedule_account_ids or [])]
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "schedule_account_ids 必须是数字列表")
    db = SessionLocal()
    try:
        u = db.get(User, uid)
        if not u:
            raise HTTPException(404, "用户不存在")
        uname = u.username
        owned = {a.id for a in db_api.list_accounts_for_user(uid, False, db)}
        bad = [x for x in aids if x not in owned]
        if bad:
            raise HTTPException(400, f"只能选该用户自己或系统共享的公众号，越权：{bad}")
        db_api.upsert_user_setting(
            uid, db=db,
            schedule_enabled=1 if body.schedule_enabled else 0,
            schedule_time=t or "10:00",
            schedule_account_ids=json.dumps(aids, ensure_ascii=False),
            schedule_product_id=int(body.schedule_product_id or 0),
        )
    finally:
        db.close()
    audit.audit(request, "schedule.admin",
                f"代管用户「{uname}」每日定时：{'开启' if body.schedule_enabled else '关闭'}，"
                f"每天 {t or '10:00'}，公众号 {aids or '默认'}，产品#{body.schedule_product_id or 0}",
                user=me, level="warn")
    return {"ok": True}


def _start_run_from_req(req: RunRequest, operator: dict = None) -> str:
    """按请求参数创建并启动后台 job（/api/run 与每日定时自动发布共用）。返回 job_id；参数不合法抛 HTTPException。"""
    # ---- 解析多主题 ----
    if req.topics:
        # topics=[{name, count?}]，每项 count 缺省用全局 req.count
        tl = []
        for t in req.topics:
            if not isinstance(t, dict):
                continue
            nm = str(t.get("name") or "").strip()
            if not nm:
                continue
            c = int(t.get("count") or req.count)
            if c < 1:
                c = 1
            tl.append({"name": nm, "count": min(c, 10)})
        if not tl:
            raise HTTPException(400, "topics 列表为空或主题名不合法")
    else:
        # 兼容旧单主题
        if not req.topic.strip():
            raise HTTPException(400, "请至少填写/勾选一个主题")
        tl = [{"name": req.topic.strip(), "count": max(1, min(req.count, 10))}]

    if req.provider not in llm.PROVIDERS:
        raise HTTPException(400, f"provider 必须是 {list(llm.PROVIDERS)} 之一")

    db = SessionLocal()
    # 每用户默认 LLM（多租户：各人用各人的模型/Key）。
    # ⚠️ 未获管理员授权(llm_use_shared)的用户，缺 Key 时**直接拒绝**，绝不静默用服务端默认 Key。
    llm_cfg, llm_err = _resolve_llm_for_user(
        operator["id"] if operator else 0, db=db,
        provider=req.provider, base_url=req.base_url, model=req.model, api_key=req.api_key)
    if llm_err:
        db.close()
        raise HTTPException(400, llm_err)
    api_key = llm_cfg["api_key"]
    model = llm_cfg["model"]
    base_url = llm_cfg["base_url"]

    product_name, product_intro, product_style = "", "", ""
    if req.product_id:
        prod = db.get(Product, req.product_id)
        if prod:
            # 与目标公众号一致的限制：只能用【自己创建的 + 公共(NULL)】产品，管理员不限
            if operator:
                _admin = operator["role"] == "admin"
                if not (_admin or prod.owner_id is None or prod.owner_id == operator["id"]):
                    db.close()
                    raise HTTPException(403, "无权使用该产品（只能选你自己创建的或公共产品）")
            product_name, product_intro, product_style = prod.name, prod.intro, (prod.style or "")
    account = None
    if req.account_id:
        account = db.get(Account, req.account_id)
        if account and operator:
            is_admin = operator["role"] == "admin"
            visible = is_admin or account.owner_id is None or account.owner_id == operator["id"]
            if not visible:
                db.close()
                raise HTTPException(403, "无权发布到该公众号（只能发你自己的或系统共享号）")
    db.close()

    brand = product_name or req.brand
    job = _new_job(tl, brand, req.author, req.extra_hint, operator=operator)
    job["provider"] = req.provider
    job["api_key"] = api_key
    job["llm_model"] = model
    job["base_url"] = base_url
    job["allow_env_key"] = bool(llm_cfg["using_shared"])   # 未获授权 → 不许回落到服务端 .env 的 Key
    job["key_source"] = llm_cfg["key_source"]
    job["use_api"] = bool(req.use_api)
    job["product_name"] = product_name
    job["product_intro"] = product_intro
    job["product_style"] = product_style
    job["source_label"] = "手动"
    job["notify_on_done"] = bool(req.notify)
    # 目标公众号账号（appid/secret/author）
    job["account_appid"] = account.appid if account else ""
    job["account_secret"] = account.secret_enc if account else ""
    if account:
        # 账号默认作者：仅当用户没显式指定不同作者时用账号 author
        job["author"] = req.author or account.author
        job["account_name"] = account.name
    else:
        job["account_name"] = "默认(代码内置)"
    with jobs_lock:
        jobs[job["id"]] = job
    threading.Thread(target=_run_job, args=(job["id"],), daemon=True).start()
    return job["id"]


@app.post("/api/run")
async def run_job(req: RunRequest, request: Request):
    me = auth.need_role(request, "editor")
    jid = _start_run_from_req(req, operator=me)
    _t = ", ".join(f"{t['name']}×{t['count']}" for t in (jobs.get(jid, {}).get("topics") or []))
    audit.audit(request, "job.run",
                f"创建发布任务 {jid}：{_t}；方式={'API 自动发布' if req.use_api else '扫码发表'}"
                f"；产品={jobs.get(jid, {}).get('product_name') or req.brand}", user=me)
    return {"job_id": jid}


# ========== 每日定时自动发布（默认每天 10:00，全主题 × 每主题 1 篇，API 免扫码） ==========
SCHED_KEY = "daily_schedule"
SCHED_DEFAULT = {
    "enabled": True,      # 默认开启
    "time": "10:00",      # 默认每天上午 10 点
    "account_id": 0,      # 目标公众号（0=取第一个账号）
    "product_id": 0,      # 产品（0=取第一个产品）
    "provider": "deepseek",
    "last_run_date": "",  # 当天已触发的日期（YYYY-MM-DD），防止同一天重复
}
SCHED_CFG: Dict = dict(SCHED_DEFAULT)


def _load_sched():
    global SCHED_CFG
    try:
        raw = json.loads(get_setting(SCHED_KEY, "{}") or "{}")
        if isinstance(raw, dict):
            SCHED_CFG = {**SCHED_DEFAULT, **{k: raw.get(k, v) for k, v in SCHED_DEFAULT.items()}}
    except Exception:
        SCHED_CFG = dict(SCHED_DEFAULT)
    # 类型兜底（旧配置可能是字符串等）
    SCHED_CFG["enabled"] = bool(SCHED_CFG["enabled"])
    SCHED_CFG["time"] = str(SCHED_CFG["time"] or "10:00")


def _save_sched():
    set_setting(SCHED_KEY, json.dumps(SCHED_CFG, ensure_ascii=False))


class ScheduleIn(BaseModel):
    enabled: bool = True
    time: str = "10:00"
    account_id: int = 0
    product_id: int = 0


def _user_count() -> int:
    """系统内用户总数（用于判断是否为「多用户部署」）。"""
    db = SessionLocal()
    try:
        return db.query(User).count()
    except Exception:  # noqa: BLE001
        return 0
    finally:
        db.close()


@app.get("/api/schedule")
async def api_schedule_get():
    """返回当前全局定时配置 + 下次触发时间文案。
    multi_user=True 表示多人部署 —— 前端据此隐藏「全局定时」老入口（改用每人独立的定时）。"""
    cfg = SCHED_CFG
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    next_text = ""
    try:
        hh, mm = [int(x) for x in str(cfg["time"]).split(":")]
        t_today = datetime(now.year, now.month, now.day, hh, mm)
        if cfg.get("enabled") and cfg.get("last_run_date") == today:
            next_dt = t_today + timedelta(days=1)
        elif cfg.get("enabled") and now >= t_today:
            # 今天已过设定时刻但还没触发过 → 会在补跑截止前自动补跑（服务运行中即刻触发）
            next_dt = now if now.hour < CATCHUP_UNTIL_HOUR else t_today + timedelta(days=1)
        elif cfg.get("enabled"):
            next_dt = t_today
        else:
            next_dt = None
        if next_dt:
            next_text = next_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        next_text = ""
    return {
        "enabled": bool(cfg["enabled"]),
        "time": cfg["time"],
        "account_id": cfg.get("account_id", 0),
        "product_id": cfg.get("product_id", 0),
        "last_run_date": cfg.get("last_run_date", ""),
        "next_run": next_text,
        "multi_user": _user_count() > 1,   # 多人部署 → 前端隐藏「全局定时」老入口
    }


@app.get("/api/notify/test")
async def api_notify_test(request: Request):
    """群机器人通知连通性测试：配置好 ROBOT_WEBHOOK_URL 后调用，手机微信应收到一条测试消息。"""
    me = auth.need_role(request, "editor")
    if not notify.configured():
        raise HTTPException(400, "未配置 ROBOT_WEBHOOK_URL（企业微信群机器人 Webhook），无法发送")
    r = notify.send_test()
    audit.audit(request, "notify.test", "发送企业微信通知连通性测试",
                user=me, result="ok" if r.get("errcode", -1) == 0 else "fail")
    if r.get("errcode", -1) == 0:
        return {"ok": True, "msg": "测试通知已发送，请查收企业微信群（手机微信可看）"}
    raise HTTPException(502, f"通知发送失败: {r.get('errmsg') or r}")


@app.post("/api/schedule")
async def api_schedule_set(cfg: ScheduleIn, request: Request):
    me = auth.need_role(request, "editor")
    t = (cfg.time or "").strip()
    if not re.fullmatch(r"(?:[01]?\d|2[0-3]):[0-5]\d", t):
        raise HTTPException(400, "时间格式应为 HH:MM，如 10:00")
    SCHED_CFG["enabled"] = bool(cfg.enabled)
    SCHED_CFG["time"] = f"{int(t.split(':')[0]):02d}:{t.split(':')[1]}"
    SCHED_CFG["account_id"] = int(cfg.account_id or 0)
    SCHED_CFG["product_id"] = int(cfg.product_id or 0)
    _save_sched()
    audit.audit(request, "settings.schedule",
                f"修改定时设置：{'开启' if SCHED_CFG['enabled'] else '关闭'}，每天 {SCHED_CFG['time']}，"
                f"公众号#{SCHED_CFG['account_id'] or '默认'}，产品#{SCHED_CFG['product_id'] or '默认'}", user=me)
    return {"ok": True, "enabled": SCHED_CFG["enabled"], "time": SCHED_CFG["time"]}


@app.post("/api/daily/run")
async def api_daily_run(request: Request):
    """手动触发一次「每日批次」（补跑用）：POST /api/daily/run
    · 今日计划已全部完成 → 拒绝（避免重复发文）
    · 否则立即在后台跑当天批次（服务常驻进程里执行，不受外部会话影响）；
      按【当前操作人】自己的 LLM 配置与公众号列表发布，不干扰其他用户。"""
    me = auth.need_role(request, "editor")
    today = datetime.now().strftime("%Y-%m-%d")
    plan = _plan_load()
    if plan.get("date") == today:
        items = plan.get("items", [])
        pending = [i for i in items if i.get("status") != "done"]
        if items and not pending:
            raise HTTPException(400, f"今日批次已完成（{len(items)} 篇），无需补跑")
    # 用操作人自己的配置跑（不传 account_ids → 由 _run_daily_batch 按其 UserSetting 解析）
    threading.Thread(target=_run_daily_batch,
                    kwargs=dict(user_id=me["id"], reason="手动补跑"), daemon=True).start()
    logger.info("手动触发每日批次（补跑）用户#%s", me["id"])
    audit.audit(request, "job.daily", f"手动补跑今日批次（{today}）", user=me)
    return {"ok": True, "msg": f"已触发每日批次（{today}），后台执行，约 25 分钟"}


# ========== 每日分批发布 ==========
# 微信 freepublish/submit 频控实测规律：
#   · 爆发会触发惩罚性限流：2026-09-10 在 4 分钟内连发 16 篇 → 触发，降级期长达约 9 小时
#     （降级期内约每 30 分钟才放行 1 次）
#   · 稳定的 1 篇/分钟 则安全：2026-09-11 手动连发 19 篇（18:25~18:50，篇间隔 65s）全部成功零限流，
#     同日定时 15+5 篇也全部成功 → 结论：只要单篇间隔 ≥60 秒，可连续发数十篇不触发。
# 因此策略：单篇间隔 POST_INTERVAL_SEC=60 秒（安全底线，切勿调快）；单批上限 BATCH_SIZE 篇
# 作为"保险丝"；一批发不完才休息 BATCH_GAP_MIN 分钟（撞限后按 35/70/120 递增）。
# 失败篇保留微信草稿(media_id)，下一批直接补发，不重新生成。
BATCH_SIZE = 25           # 每批最多发布篇数（60s/篇下 25 篇≈27分钟，速率仍远低于触发阈值）
POST_INTERVAL_SEC = 60    # 批内两篇提交之间的间隔（秒）——≈1 次/分钟，这是安全底线，不要调快
CATCHUP_UNTIL_HOUR = 20   # 定时窗口错过后的补跑截止小时（晚于该点不再补跑，避免深夜发文）
BATCH_GAP_MIN = 30        # 批间休息（分钟，全成功批用）；失败批固定 60 分钟，封顶 60（用户要求每 30~60 分钟巡检一次）
PLAN_KEY = "publish_plan"
_batch_lock = threading.Lock()


def _plan_load() -> dict:
    try:
        p = json.loads(get_setting(PLAN_KEY, "{}") or "{}")
        return p if isinstance(p, dict) else {}
    except Exception:
        return {}


def _plan_save(p: dict):
    set_setting(PLAN_KEY, json.dumps(p, ensure_ascii=False))


def _plan_enqueue_items(new_items: list, gap_min: int = 35) -> int:
    """把手动发布未成功的篇并入发布计划队列，由后台调度线程自动补发（持久化，不怕重启）。
    new_items: [{"topic","title","body_path","draft_mid","status":"pending"}]；返回入队数量。"""
    today = datetime.now().strftime("%Y-%m-%d")
    plan = _plan_load()
    if plan.get("date") != today:
        plan = {"date": today, "items": [], "next_ts": 0, "batch_done": 0, "fail_streak": 0}
    exist = {(i.get("title") or "") for i in plan.get("items", []) if i.get("status") != "done"}
    added = 0
    for it in new_items:
        t = (it.get("title") or "").strip()
        if not t or t in exist:
            continue
        plan.setdefault("items", []).append({
            "topic": it.get("topic") or "", "title": t,
            "body_path": it.get("body_path") or "", "draft_mid": it.get("draft_mid") or "",
            "status": "pending",
        })
        exist.add(t)
        added += 1
    if added and plan.get("items"):
        nxt = int(time.time()) + gap_min * 60
        plan["next_ts"] = nxt if not plan.get("next_ts") else min(int(plan["next_ts"]), nxt)
    _plan_save(plan)
    return added


def _today_done_topics(aid: int = 0) -> set:
    """今天（北京时间）某公众号已成功发布的主题集合（aid=0 表示全部号），避免多号/续批重复发布。"""
    bj_now = datetime.utcnow() + timedelta(hours=8)
    bj_day_start = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = bj_day_start - timedelta(hours=8)
    db = SessionLocal()
    try:
        q = db.query(Article).filter(Article.created_at >= utc_start, Article.status == "published")
        if aid:
            q = q.filter(Article.account_id == aid)
        rows = q.all()
        return {r.topic for r in rows if r.topic}
    finally:
        db.close()


def _today_published_count() -> int:
    """微信端今天（北京时间）已发布的文章篇数。

    多设备同时使用同一公众号时，本机数据库看不到另一台机器的发布记录，
    但微信端记录是同一份 —— 据此可发现"今天是否已被别的设备发过"。
    返回 -1 表示查询失败（调用方应跳过跨设备判断，避免误跳过）。
    """
    try:
        return len(wechat_api.fetch_today_published(40))
    except Exception as e:  # noqa: BLE001
        logger.warning("查询微信端今日已发篇数失败：%s", str(e)[:100])
        return -1


def _today_published_local_count(aid: int = 0) -> int:
    """本机数据库记录的"今天（北京时间）某公众号已发布"篇数（用于与微信端对比，发现外部发布）。"""
    bj_now = datetime.utcnow() + timedelta(hours=8)
    utc_start = bj_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    db = SessionLocal()
    try:
        q = db.query(Article).filter(Article.created_at >= utc_start,
                                    Article.status == "published")
        if aid:
            q = q.filter(Article.account_id == aid)
        return q.count()
    finally:
        db.close()


QUEUE_KEY = "gen_queue"   # 已生成未发布的文章队列（先发库存、发完再生成）


def _load_gen_queue() -> list:
    try:
        q = json.loads(get_setting(QUEUE_KEY, "[]") or "[]")
        return q if isinstance(q, list) else []
    except Exception:
        return []


def _save_gen_queue(q: list):
    set_setting(QUEUE_KEY, json.dumps(q, ensure_ascii=False))


def _plan_init_today(topics: list, account_ids=None) -> dict:
    # 多租户：某主题只有"所有目标号今天都发过"才视为已发（避免 A 号发过就把 B 号也跳过）
    accs = [int(x) for x in (account_ids or [])]
    if accs:
        done_sets = [_today_done_topics(a) for a in accs]
        done = set.intersection(*done_sets) if done_sets else set()
    else:
        done = _today_done_topics(0)
    # ── 跨设备防重 ───────────────────────────────────────────────
    wx_n = _today_published_count()
    local_n = _today_published_local_count(accs[0] if accs else 0)
    if wx_n > local_n:
        extra = min(wx_n - local_n, len(topics))
        if extra > 0:
            skipped = list(topics[:extra])
            done = done | set(skipped)
            logger.warning("跨设备防重：微信端今日已发 %d 篇、本机记录 %d 篇 → 跳过前 %d 个主题：%s",
                           wx_n, local_n, extra, "、".join(skipped))
    items = []
    queue = _load_gen_queue()
    if queue:
        taken, rest = 0, []
        for it in queue:
            tp = (it.get("topic") or "").strip()
            if taken < len(topics) and tp and tp not in done:
                items.append({"topic": tp, "title": it.get("title") or "",
                              "body_path": it.get("body_path") or "",
                              "draft_mid": "", "drafts": {}, "status": "pending"})
                taken += 1
            else:
                rest.append(it)
        _save_gen_queue(rest)
        if taken:
            logger.info("从已生成队列取 %d 篇今日发布（队列剩余 %d 篇）", taken, len(rest))
    covered = {it["topic"] for it in items}
    items += [{"topic": nm, "title": "", "draft_mid": "", "drafts": {}, "status": "pending"}
              for nm in topics if nm not in done and nm not in covered]
    return {"date": datetime.now().strftime("%Y-%m-%d"), "items": items, "next_ts": 0, "batch_done": 0,
            "fail_streak": 0, "limit_alerted": False}


def _item_done_for_accounts(it: dict, target_ids: list) -> bool:
    """某主题是否已发到【全部】目标公众号（多租户去重判据）。

    单号去重由微信端 _today_done_topics 负责；这里的 done_accounts 记录本计划里
    该主题已成功发到哪些号，只有覆盖全部目标号才视为 plan 级 done，避免
    「A 号发过就把 B 号也跳过」的错误。"""
    if not target_ids:
        return False
    das = set(int(x) for x in (it.get("done_accounts") or []))
    return set(int(x) for x in target_ids) <= das


def _topic_names_for_product(topic_rows, product_id: int) -> list:
    """按产品过滤主题名：保留【该产品专属(product_id==pid)】与【通用(0/未设置)】的主题。
    用途：定时发布只发当前产品对应的主题 —— 保健功效类主题绑到木梳后，就不会出现在美食产品下（2026-10-16）。"""
    pid = int(product_id or 0)
    return [t.name for t in topic_rows if (t.product_id or 0) in (0, pid)]


def _run_daily_batch(user_id=None, account_ids=None, reason="每日定时", schedule_product_id=0,
                     mark_daily_run=False):
    """执行一个批次：把今天的发布计划发到「用户勾选的多个公众号」（多租户）。
    - 每个用户用自己的 LLM 配置（各人消耗各人的 Key）；每个公众号独立去重/草稿/发表；
    - 同一批主题会发到用户勾选的每一个号 ⇒ 实现"多个公众号同时定时发布"；
    - 结束后还有剩余 → 30/60 分钟自动续批；全部完成 → 清计划；结果推送到微信群。
    - mark_daily_run=True 时（调度器每日起点拉起），会在拿到锁后写入该用户
      schedule_last_run_date，确保同一天不会被重复拉起。"""
    if not _batch_lock.acquire(blocking=False):
        logger.info("已有批次在执行，跳过本次触发")
        return
    try:
        # 多租户每日触发：标记该用户今日已触发，避免调度器重复拉起（仅在真正拿到锁后写）
        if mark_daily_run and user_id:
            try:
                _db = SessionLocal()
                try:
                    _us = _db.query(UserSetting).filter(UserSetting.user_id == user_id).first()
                    if _us:
                        _us.schedule_last_run_date = datetime.now().strftime("%Y-%m-%d")
                        _db.commit()
                finally:
                    _db.close()
            except Exception as e:
                logger.warning("更新 schedule_last_run_date 失败: %s", e)
        _ensure_card_render_ok("每日批次开始")   # 渲染管线自检：不可用立即告警，避免静默降级
        db = SessionLocal()
        try:
            topic_rows = db.query(Topic).order_by(Topic.id).all()
            all_accounts = db.query(Account).order_by(Account.id).all()
        finally:
            db.close()

        # ── 解析目标公众号列表（多租户：优先用传入 account_ids，否则从用户设置取）──
        target_accounts = []
        if account_ids:
            target_accounts = [a for a in all_accounts if a.id in set(int(x) for x in account_ids)]
        else:
            us = db_api.get_user_setting(user_id) if user_id else None
            if not us and all_accounts:
                us = db_api.get_user_setting(all_accounts[0].owner_id) if all_accounts[0].owner_id else None
            if us:
                aids = []
                try:
                    aids = [int(x) for x in json.loads(us.schedule_account_ids or "[]")] if us.schedule_account_ids else []
                except Exception:
                    aids = []
                if not aids and us.default_account_id:
                    aids = [us.default_account_id]
                target_accounts = [a for a in all_accounts if a.id in set(aids)]
            if not target_accounts and all_accounts:
                target_accounts = [all_accounts[0]]   # legacy 兜底：第一个号
        if not target_accounts:
            logger.warning("定时发布跳过：无任何可发布公众号")
            return
        target_ids = [a.id for a in target_accounts]

        # ── 解析该用户的 LLM 配置（各人消耗各人的 Key + 管理员授权才可用共享 Key）──
        # 用户 id 缺失时（旧版全局定时）回落到目标号主的设置，保持向后兼容
        _owner_uid = user_id
        if not _owner_uid and target_accounts:
            _owner_uid = target_accounts[0].owner_id or 0
        if not _owner_uid:
            # 旧版全局定时（无人启用每用户定时）：视作服务端所有者（管理员）在跑 —— 向后兼容，
            # 否则会因为「查不到用户」而被新策略拦下，把老的单用户部署直接搞停。
            _db = SessionLocal()
            try:
                _a = _db.query(User).filter(User.role == "admin").order_by(User.id).first()
                _owner_uid = _a.id if _a else 0
            finally:
                _db.close()
        llm_cfg, llm_err = _resolve_llm_for_user(_owner_uid)
        prov = llm_cfg["provider"]; model = llm_cfg["model"]; base_url = llm_cfg["base_url"]
        api_key = llm_cfg["api_key"]
        if llm_err:
            # 硬拦：不静默用默认 Key。落一条失败任务（前端可见）+ 审计 + 企微告知，当天不再重试
            _jid = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
            _op = None
            if _owner_uid:
                _db = SessionLocal()
                try:
                    _u = _db.query(User).filter(User.id == _owner_uid).first()
                    if _u:
                        _op = {"username": _u.username, "role": _u.role}
                finally:
                    _db.close()
            with jobs_lock:
                jobs[_jid] = {
                    "id": _jid, "topics": [], "topic": "", "total": 0, "count": 0,
                    "brand": "", "author": "", "extra_hint": "",
                    "created_at": datetime.now().isoformat(), "phase": "failed", "articles": [],
                    "log": [f"[{datetime.now().isoformat()}] ⛔ {reason}：{llm_err}"],
                    "provider": prov, "api_key": "", "llm_model": model, "base_url": base_url,
                    "use_api": True, "product_name": "", "product_intro": "", "product_style": "",
                    "account_appid": "", "account_secret": "",
                    "account_name": "、".join(a.name for a in target_accounts) or "默认",
                    "source_label": "每日定时", "notify_on_done": False,
                    "operator": _op or {"username": "系统定时", "role": "system"},
                    "cancel": True, "error": llm_err,
                }
            logger.warning("每日批次跳过（用户#%s）：%s", _owner_uid, llm_err)
            audit.audit(None, "publish.skipped",
                        f"每日批次（{reason}）已跳过：{llm_err}",
                        user=_op or {"username": "系统定时", "role": "system"}, level="warn")
            try:
                if notify.configured():
                    notify.send("## ⏭ 今日未发布：缺少 API Key",
                                f"{(('用户：' + _op['username']) if _op else '')}\n\n{llm_err}\n\n"
                                f"（修复后当天可手动补跑；未授权用户不会使用服务端默认 Key）",
                                use_markdown=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("跳过通知失败：%s", e)
            return

        # ── 产品（定时使用的产品）──
        product = None
        if schedule_product_id:
            db = SessionLocal()
            try:
                product = db.query(Product).filter(Product.id == schedule_product_id).first()
            finally:
                db.close()
        if not product:
            db = SessionLocal()
            try:
                product = db.query(Product).order_by(Product.id).first()
            finally:
                db.close()

        # ── 主题按产品过滤：只发【该产品专属 + 通用(product_id=0)】的主题 ──
        # 把保健功效类主题绑定到木梳后，它就不会再出现在美食产品的定时发布里（2026-10-16 新增）
        topics = _topic_names_for_product(topic_rows, product.id if product else 0)
        if not topics:
            logger.warning("定时发布跳过：产品「%s」下无可用主题（专属与通用均为空）",
                           (product.name if product else "默认产品"))
            return

        plan = _plan_load()
        today = datetime.now().strftime("%Y-%m-%d")
        if plan.get("date") != today:
            # 新的一天：重建计划，但保留昨天尚未补发完的手动发布篇（有正文/草稿的），避免丢失
            leftover = [it for it in plan.get("items", [])
                        if it.get("status") != "done" and (it.get("body_path") or it.get("draft_mid") or it.get("drafts"))]
            plan = _plan_init_today(topics, [a.id for a in target_accounts])
            if leftover:
                plan["items"] = leftover + plan["items"]
                logger.info("跨天保留未补发的 %d 篇（有正文/草稿）", len(leftover))
        plan["items"] = [it for it in plan.get("items", [])
                         if it.get("topic") in topics or it.get("draft_mid") or it.get("drafts") or it.get("body_path")]
        pending = [it for it in plan["items"] if not _item_done_for_accounts(it, target_ids)]
        if not pending:
            logger.info("今日发布计划已全部完成")
            plan["next_ts"] = 0
            _plan_save(plan)
            return

        batch = pending[:BATCH_SIZE]
        batch_no = int(plan.get("batch_done", 0)) + 1
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        acc_names = "、".join(a.name for a in target_accounts) or "默认"
        # 操作人（审计用）
        op_user = None
        if user_id:
            db = SessionLocal()
            try:
                u = db.query(User).filter(User.id == user_id).first()
                if u:
                    op_user = {"username": u.username, "role": u.role}
            finally:
                db.close()
        job = {
            "id": job_id,
            "topics": [{"name": it["topic"], "count": 1} for it in batch],
            "topic": batch[0]["topic"] if batch else "",
            "total": len(batch), "count": len(batch),
            "brand": product.name if product else "百秘一梳",
            "author": target_accounts[0].author if target_accounts else "百秘一梳",
            "extra_hint": "",
            "created_at": datetime.now().isoformat(),
            "phase": "generating",
            "articles": [],
            "log": [f"[{datetime.now().isoformat()}] ⏰ {reason}：第 {batch_no} 批开始（本批 {len(batch)} 篇，计划剩余 {len(pending)} 篇，目标号：{acc_names}）"
                    + ("" if notify.configured() else "｜⚠️ 未配置 ROBOT_WEBHOOK_URL，完成通知将跳过")],
            "provider": prov, "api_key": api_key, "llm_model": model, "base_url": base_url,
            "allow_env_key": bool(llm_cfg["using_shared"]),   # 未获授权 → 不许回落到服务端 .env 的 Key
            "key_source": llm_cfg["key_source"],
            "use_api": True,
            "product_name": product.name if product else "",
            "product_intro": product.intro if product else "",
            "product_style": (product.style or "") if product else "",
            "account_appid": target_accounts[0].appid if target_accounts else "",
            "account_secret": target_accounts[0].secret_enc if target_accounts else "",
            "account_name": acc_names,
            "source_label": "每日定时", "notify_on_done": True,
            "operator": op_user or {"username": "系统定时", "role": "system"},
            "cancel": False,
        }
        with jobs_lock:
            jobs[job_id] = job

        # 先确保本批每篇都有正文（生成一次，各号复用，避免重复消耗 LLM）
        for it in batch:
            if it.get("body_path"):
                f = ARTICLES_DIR / Path(it["body_path"]).name
                if f.exists():
                    it["_body"] = f.read_text(encoding="utf-8")
                    it["_title"] = it.get("title") or ""
            if not it.get("_body"):
                try:
                    items = _gen_with_dedup(it["topic"], 1, job, job["log"])
                except Exception as e:
                    job["log"].append(f"⚠️ 主题「{it['topic']}」生成失败（本批跳过，下批重试）：{str(e)[:100]}")
                    it["_body"] = None
                    continue
                if not items:
                    it["_body"] = None
                    continue
                it["_title"] = items[0]["title"]; it["_body"] = items[0]["body_md"]
                md_p = ARTICLES_DIR / f"{job_id}_{it['topic']}_{uuid.uuid4().hex[:4]}.md"
                md_p.write_text(it["_body"], encoding="utf-8")
                it["title"] = it["_title"]; it["body_path"] = md_p.name
                try:
                    dbg = SessionLocal()
                    dbg.add(Article(job_id=job_id, seq=-1, topic=it["topic"], title=it["_title"],
                                   body_md=it["_body"], status="generated"))
                    dbg.commit(); dbg.close()
                except Exception:
                    pass
            # 排版（各号复用同一份卡片）
            body_html, _tmid, _tmname = wxcard.render_md_with_theme_safe(it["_body"])
            it["_html"] = body_html; it["theme"] = _tmname

        ok_total = 0
        hit_limit = False
        ip_blocked = False   # 40164：本机公网 IP 不在公众号白名单（需人工去后台加白）
        acc_done = {}
        for acc in target_accounts:
            try:
                wechat_api.set_account(acc.appid, acc.secret_enc)
            except Exception as e:
                job["log"].append(f"⚠️ 切换公众号「{acc.name}」失败：{e}")
                continue
            acc_done[acc.id] = _today_done_topics(acc.id)
            # 跨设备双保险：该号微信端今日已发 ≥ 本批篇数 → 跳过该号（另一设备可能已发完）
            wx_n = _today_published_count()
            if wx_n > 0 and wx_n >= len(batch):
                job["log"].append(f"  ℹ️ 公众号「{acc.name}」：微信端今日已发 {wx_n} 篇 ≥ 本批 {len(batch)} 篇，跳过该号（另一设备可能已发）")
                continue
            job["log"].append(f"  📡 开始向公众号「{acc.name}」发布本批 {len(batch)} 篇")
            acc_ok = 0
            k = 0
            for it in batch:
                topic = it["topic"]
                if topic in acc_done[acc.id]:
                    continue   # 该号今天已发过此主题
                if not it.get("_body"):
                    continue
                seq = len(job["articles"])
                with jobs_lock:
                    job["phase"] = f"publishing_p{seq+1}"
                    job["log"].append(f"[{datetime.now().isoformat()}] 「{acc.name}」第 {k+1}/{len(batch)} 篇：{topic}")
                job["articles"].append({"seq": seq, "account": acc.name, "topic": topic,
                                        "title": it.get("_title") or topic, "status": "publishing"})
                try:
                    mid = (it.get("drafts") or {}).get(str(acc.id)) or ""
                    title = it.get("_title") or topic
                    if not mid:
                        # 每个号独立存草稿（草稿是按号隔离的，不能跨号复用）
                        mid, _cover_note = _add_draft_with_cover_retry(
                            title=title, body_html=it.get("_html") or "", author=job["author"],
                            digest=(it["_body"] or "")[:100])
                        if _cover_note:
                            job["log"].append(f"  {_cover_note}")
                        it.setdefault("drafts", {})[str(acc.id)] = mid
                    job["log"].append(f"  提交发布到「{acc.name}」（草稿 {str(mid)[:12]}…）")
                    pub = wechat_api.publish_draft(mid, fast_fail=True)   # 批次内快速失败，失败篇留待下一批
                    pid = str(pub.get("publish_id") or "")
                    # 记录该主题已成功发到本号；只有覆盖全部目标号才在收尾时判为 plan 级 done
                    it.setdefault("done_accounts", [])
                    if acc.id not in it["done_accounts"]:
                        it["done_accounts"].append(acc.id)
                    job["articles"][seq]["status"] = "published"
                    job["articles"][seq]["publish_id"] = pid
                    # 落库（带 account_id，多号去重用）
                    try:
                        db3 = SessionLocal()
                        row = db3.query(Article).filter(Article.job_id == job_id, Article.seq == seq).first()
                        if row:
                            row.status = "published"; row.publish_id = pid; row.account_id = acc.id
                        else:
                            db3.add(Article(job_id=job_id, seq=seq, topic=topic, title=title,
                                           body_md=it["_body"], status="published", publish_id=pid,
                                           account_id=acc.id, theme=it.get("theme", "")))
                        db3.commit(); db3.close()
                    except Exception as _e:
                        logger.warning("批次发布结果落库失败: %s", str(_e)[:100])
                    ok_total += 1; acc_ok += 1
                    job["log"].append(f"✅ 「{acc.name}」第 {k+1} 篇发布成功 publish_id={pid}")
                except Exception as e:
                    job["articles"][seq]["status"] = "failed"
                    job["articles"][seq]["error"] = str(e)[:200]
                    job["log"].append(f"❌ 「{acc.name}」第 {k+1} 篇失败（留待下一批补发）：{str(e)[:120]}")
                    if "40164" in str(e):
                        ip_blocked = True
                    if "53501" in str(e):
                        hit_limit = True
                        job["log"].append("  ⚠️ 命中微信频率限制 → 本号本批提前结束，剩余篇留到下一批")
                        break
                k += 1
                if k < len(batch):
                    time.sleep(POST_INTERVAL_SEC)   # 组内篇间隔，避免密集提交触发惩罚性限流
            job["log"].append(f"  ✅ 公众号「{acc.name}」本批成功 {acc_ok} 篇")

        # 收尾：重新计算每篇完成状态（多租户：发到全部目标号才算 done）
        for it in plan["items"]:
            if _item_done_for_accounts(it, target_ids):
                it["status"] = "done"
            elif it.get("_body") or it.get("drafts"):
                it["status"] = "partial"
            else:
                it["status"] = "pending"
        remaining = [it for it in plan["items"] if it.get("status") != "done"]
        plan["batch_done"] = batch_no
        # 连续全失败时逐次拉长批间隔（30→60 分钟，封顶 60），避免无效高频重试
        if ok_total > 0:
            plan["fail_streak"] = 0
        else:
            plan["fail_streak"] = int(plan.get("fail_streak", 0)) + 1
        if remaining and datetime.now().hour < 23:
            fs = int(plan.get("fail_streak", 0))
            # 用户要求每 30~60 分钟巡检一次直到当天全部发布成功：
            # 全成功批→休 BATCH_GAP_MIN(30) 分钟（多于 25 篇需分批时也安全）；失败批→休 60 分钟，封顶 60，避免深夜前无限拖延
            gap_min = BATCH_GAP_MIN if fs < 1 else 60
            gap_min = min(gap_min, 60)
            plan["next_ts"] = int(time.time()) + gap_min * 60
        else:
            plan["next_ts"] = 0
        _plan_save(plan)
        nxt_txt = datetime.fromtimestamp(plan["next_ts"]).strftime("%H:%M") if remaining else ""
        with jobs_lock:
            job["phase"] = "done"
            job["log"].append(f"本批完成：成功 {ok_total}/{len(batch)}；今日剩余 {len(remaining)} 篇"
                              + (f"，下一批 {nxt_txt} 自动继续" if remaining else "，全部完成 🎉"))
        # 通知前先回填文章链接（一次 batchget 取最近 50 篇；逐篇 freepublish/get 会耗尽日配额）
        _backfill_urls(job_id)
        # ===== 通知策略（2026-10-05 改为「每天一条」）=====
        # · 当天全部发完 → 把当天所有文章合并成【一条】汇总推送（短链 + 主题，实测 20 篇≈3KB，单条可达）
        # · 当天还没发完 → 不逐批打扰；仅在硬故障时单独告警（40164 IP 白名单 / 首次撞限 53501 / 23 点收尾）
        try:
            if notify.configured():
                if not remaining:
                    r = _build_today_summary_items()
                    if r:
                        head = f"## 📰 润致发布 · {datetime.now().strftime('%m月%d日')} 今日全部完成：共 {len(r)} 篇"
                        tail = f"🎉 全部主题已发完{'（共 %d 批）' % batch_no if batch_no > 1 else ''}"
                        notify.send_daily_summary(head, r, tail)
                else:
                    # 未完成时才可能发告警（保持"一天一条"的降噪目标）
                    if ip_blocked:
                        notify.send("## 🚨 发布失败：IP 白名单拦截（40164）",
                                    "微信报 40164：本机公网 IP 不在公众号后台 IP 白名单，重试无效。\n"
                                    "请到 mp.weixin.qq.com → 设置与开发 → 基本配置 → IP白名单 加上日志里的 IP。",
                                    use_markdown=True)
                    elif hit_limit and not plan.get("limit_alerted"):
                        plan["limit_alerted"] = True
                        _plan_save(plan)
                        notify.send("## ⏳ 触发微信频率限制，本批提前结束",
                                    f"已发 {ok_total} 篇，剩余 {len(remaining)} 篇将在 {nxt_txt} 自动重试（无需处理）。",
                                    use_markdown=True)
                    elif datetime.now().hour >= 23:
                        r = _build_today_summary_items()
                        if r:
                            notify.send_daily_summary(
                                f"## 📰 润致发布 · 今日收尾：已发 {len(r)} 篇",
                                r, f"⏳ 今日还剩 {len(remaining)} 篇，将于明天继续")
        except Exception as e:
            logger.warning("批次通知失败：%s", e)
        logger.info("批次完成 job=%s 成功=%d/%d 剩余=%d", job_id, ok_total, len(batch), len(remaining))
        audit.audit(None, "publish.done",
                    f"每日批次 {job_id}（{reason}）完成：成功 {ok_total}/{len(batch)} 篇，今日剩余 {len(remaining)} 篇",
                    user={"username": (job.get("operator") or {}).get("username") or ("系统" if reason != "手动补跑" else "手动补跑"),
                          "role": (job.get("operator") or {}).get("role", "")},
                    level="warn" if ok_total < len(batch) else "info")
    finally:
        _batch_lock.release()


def _fetch_urls_with_retry(publish_ids, tries: int = 5, wait: float = 6.0) -> dict:
    """取「publish_id → 文章链接」映射，带重试。

    ⚠️ 微信「发表记录」有收录延迟：刚提交成功的文章立刻查不到（2026-10-06 实测，
    末篇发布 2 秒后就汇总，导致通知里那一篇没有链接）。这里重试到全部拿到或超时，
    最多等 (tries-1)×wait 秒（默认 24 秒），拿不到的只影响该篇链接，不影响发送。
    """
    want = {str(x) for x in (publish_ids or []) if x}
    got = {}
    if not want:
        return got

    def _pull():
        try:
            m = wechat_api.fetch_recent_urls(50).get("by_mid") or {}
            for k, v in m.items():
                if k in want and k not in got:
                    got[k] = v
        except Exception as e:
            logger.warning("取文章链接失败：%s", str(e)[:100])

    _pull()
    for i in range(max(0, tries - 1)):
        if len(got) >= len(want):
            break
        time.sleep(wait)
        _pull()
    if len(got) < len(want):
        logger.warning("仍有 %d/%d 篇未取到链接（微信收录延迟，稍后可在网页刷新链接）",
                       len(want) - len(got), len(want))
    return got


def _build_today_summary_items() -> list:
    """汇总「今天（北京时间）已发布」的文章 → [{title, url, theme, topic, status}]，供每日一条通知使用。
    链接用一次 batchget 批量取（publish_id → url），主题从发布计划里读（批次执行时写入）。"""
    bj = datetime.utcnow() + timedelta(hours=8)
    utc_start = bj.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    db = SessionLocal()
    try:
        rows = (db.query(Article).filter(Article.created_at >= utc_start, Article.status == "published")
                .order_by(Article.id).all())
        items = [{"title": r.title or r.topic or "", "topic": r.topic or "",
                  "publish_id": str(r.publish_id or ""), "status": "published",
                  "theme": (r.theme or "")} for r in rows]
    finally:
        db.close()
    if not items:
        return []
    theme_by_title = {}
    try:
        for it in _plan_load().get("items", []):
            if it.get("title") and it.get("theme"):
                theme_by_title[it["title"]] = it["theme"]
    except Exception:
        pass
    urls = {}
    try:
        urls = _fetch_urls_with_retry([it["publish_id"] for it in items])   # 带重试，避免末篇漏链接
    except Exception as e:
        logger.warning("汇总取链接失败（将不带链接）：%s", str(e)[:100])
    for it in items:
        # 优先用库里记的主题（手动/定时发布都会写库）；旧数据或扫码发布无主题时，回退到发布计划
        it["theme"] = it.get("theme") or theme_by_title.get(it["title"], "")
        it["url"] = urls.get(it["publish_id"], "")
    return items


def _current_public_ip(timeout: int = 10) -> str:
    """查询本机公网出口 IP（仅标准库）。

    注意：必须用【国内】探测源。api.weixin.qq.com 是境内域名，走的是国内直连线路；
    而 ipify/ifconfig.me 等国外站点在配了代理/VPN 的机器上会被路由到代理节点，
    返回的 IP 与微信实际看到的完全不是同一个（曾因此误判）。
    主源 myip.ipip.net（返回"当前 IP：x.x.x.x 来自于：中国 …"），兜底 3322.org。
    """
    import urllib.request
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for url in ("https://myip.ipip.net", "http://members.3322.org/dyndns/getip", "https://4.ipw.cn"):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8.21.0"})
            with opener.open(req, timeout=timeout) as r:
                txt = r.read().decode("utf-8", "ignore").strip()
            m = re.search(r"(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})", txt)
            if m:
                return m.group(1)
        except Exception:
            continue
    return ""


_IP_CHECK_STATE = {"ts": 0.0}


def _ip_watchdog():
    """每 30 分钟检测一次公网出口 IP；发生变更立即企业微信告警（提示更新 IP 白名单）。
    背景：家宽公网 IP 几天一变，IP 不在白名单时发布全部 40164 失败。"""
    now = time.time()
    if now - _IP_CHECK_STATE["ts"] < 1800:
        return
    _IP_CHECK_STATE["ts"] = now
    ip = _current_public_ip()
    if not ip:
        return
    last = get_setting("last_public_ip", "")
    set_setting("last_public_ip", ip)
    if not last or ip == last:
        return
    logger.warning("公网 IP 变更：%s → %s", last, ip)
    try:
        if notify.configured():
            notify.send_markdown_chunks(
                "## 🌐 公网 IP 已变更",
                [f"旧 IP：{last}",
                 f"**新 IP：{ip}**",
                 "",
                 "IP 白名单若未包含新 IP，定时发布会整批失败（40164）。",
                 "请到 mp.weixin.qq.com → 设置与开发 → 基本配置 → IP白名单 更新",
                 "（支持 IP 段格式，如 106.85.0.0/16，加了网段就不用频繁改）。"])
    except Exception as e:
        logger.warning("IP 变更通知失败：%s", e)


def _migrate_account_owners():
    """旧库兼容：把未归属(owner_id NULL)的公众号过户给管理员，使多租户改造后单用户部署不中断。"""
    try:
        db = SessionLocal()
        try:
            n = db.query(Account).filter(Account.owner_id.is_(None)).count()
            if n == 0:
                return
            admin = db.query(User).filter(User.role == "admin").order_by(User.id).first()
            if not admin:
                return
            db.query(Account).filter(Account.owner_id.is_(None)).update({Account.owner_id: admin.id})
            db.commit()
            logger.info("已将 %d 个未归属公众号过户给管理员(uid=%d)", n, admin.id)
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("账号过户迁移失败（可忽略）: %s", e)


def _parse_aids(s) -> list:
    """把 UserSetting.schedule_account_ids（JSON 文本）解析成 int 列表（容错）。"""
    try:
        return [int(x) for x in json.loads(s or "[]")] if s else []
    except Exception:
        return []


def _parse_hhmm(t):
    """'HH:MM' → (hh, mm)，容错默认 10:00。"""
    try:
        hh, mm = [int(x) for x in str(t or "10:00").split(":")]
        return hh, mm
    except Exception:
        return 10, 0


def _enabled_user_schedules():
    """返回所有启用每日定时的 UserSetting（无则空列表）。"""
    try:
        db = SessionLocal()
        try:
            return db.query(UserSetting).filter(UserSetting.schedule_enabled == 1).all()
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("读取用户定时设置失败：%s", e)
        return []


def _scheduler_loop():
    """后台调度线程（每 20s，多租户）：
    1) 每日定时：遍历【每位启用定时】的用户，到达其设定时刻（含 3 分钟窗口/错过后补跑）
       → 用该用户自己的 LLM 配置 + 自选公众号列表拉起当天第 1 批；
       没有任何用户启用定时时，退回到旧版单全局 SCHED_CFG（向后兼容）。
    2) 续批：发布计划 next_ts 到点 → 为【每位仍有剩余主题】的启用用户继续发
       （无启用用户则用全局兜底），用分批避开微信频率风控；全程串行（单锁），
       多个用户/多个号不会并发撞微信限流。"""
    while True:
        try:
            _ip_watchdog()
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            us_rows = _enabled_user_schedules()
            # 1) 每日定时起点（含错过后补跑）—— 仅在当前无批次运行时派发，避免线程堆积
            if us_rows and not _batch_lock.locked():
                for us in us_rows:
                    if us.schedule_last_run_date == today:
                        continue   # 该用户今天已触发过
                    hh, mm = _parse_hhmm(us.schedule_time)
                    target_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if target_dt <= now:
                        in_window = now <= target_dt + timedelta(minutes=3)
                        before_cutoff = now.hour < CATCHUP_UNTIL_HOUR
                        if in_window or before_cutoff:
                            aids = _parse_aids(us.schedule_account_ids)
                            if not aids and us.default_account_id:
                                aids = [us.default_account_id]
                            reason = "每日定时" if in_window else f"补跑（服务 {now.strftime('%H:%M')} 才运行）"
                            logger.info("触发每日批次（用户#%s）：%s，目标号=%s", us.user_id, reason, aids or "默认")
                            threading.Thread(target=_run_daily_batch,
                                            kwargs=dict(user_id=us.user_id, account_ids=aids,
                                                        reason=reason, schedule_product_id=us.schedule_product_id,
                                                        mark_daily_run=True), daemon=True).start()
            elif not us_rows and SCHED_CFG.get("enabled"):
                # 向后兼容：旧版单全局定时（无人启用每用户定时时生效）
                try:
                    hh, mm = [int(x) for x in str(SCHED_CFG["time"]).split(":")]
                    target_dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
                    if SCHED_CFG.get("last_run_date") != today and target_dt <= now:
                        in_window = now <= target_dt + timedelta(minutes=3)
                        before_cutoff = now.hour < CATCHUP_UNTIL_HOUR
                        if in_window or before_cutoff:
                            SCHED_CFG["last_run_date"] = today
                            _save_sched()
                            reason = "每日定时" if in_window else f"补跑（服务 {now.strftime('%H:%M')} 才运行）"
                            logger.info("触发每日批次（全局）：%s", reason)
                            threading.Thread(target=_run_daily_batch, args=(reason,), daemon=True).start()
                        else:
                            logger.warning("今日定时已错过且已过 %d 点，不再补跑（避免深夜发文）", CATCHUP_UNTIL_HOUR)
                            SCHED_CFG["last_run_date"] = today      # 标记已处理，避免反复判断
                            _save_sched()
                except Exception as e:
                    logger.warning("定时检查异常: %s", e)
            # 2) 续批（仅在当前无批次运行时派发）
            if not _batch_lock.locked():
                plan = _plan_load()
                nxt = int(plan.get("next_ts") or 0)
                if nxt and plan.get("date") == today and time.time() >= nxt:
                    plan["next_ts"] = 0      # 先清零防重复触发；批次结束会重新写
                    _plan_save(plan)
                    if us_rows:
                        # 为每位【仍有剩余主题】的启用用户继续发（跳过已发完的，避免空跑）
                        for us in us_rows:
                            aids = _parse_aids(us.schedule_account_ids)
                            if not aids and us.default_account_id:
                                aids = [us.default_account_id]
                            if not aids:
                                continue
                            has_remaining = any(not _item_done_for_accounts(it, aids)
                                                for it in plan.get("items", []))
                            if not has_remaining:
                                continue
                            threading.Thread(target=_run_daily_batch,
                                            kwargs=dict(user_id=us.user_id, account_ids=aids,
                                                        reason="续批"), daemon=True).start()
                    else:
                        threading.Thread(target=_run_daily_batch, args=("续批",), daemon=True).start()
        except Exception:
            logger.exception("scheduler loop error")
        time.sleep(20)


@app.get("/")
async def index():
    return FileResponse(str(APP_DIR / "static" / "index.html"))


def _topics_label(topics) -> str:
    """把多主题概要成一句话，如『偏头痛×2、失眠×1』"""
    if not topics:
        return ""
    return "、".join(f"{t.get('name','')}×{t.get('count',1)}" for t in topics)


@app.get("/api/jobs")
async def list_jobs():
    out = []
    with jobs_lock:
        for j in jobs.values():
            out.append({
                "id": j["id"], "topic": j["topic"], "topics_label": _topics_label(j.get("topics")),
                "phase": j["phase"],
                "count": j["count"], "created_at": j["created_at"],
            })
    # 倒序：最新（最大 created_at）在前
    out.sort(key=lambda x: x["created_at"], reverse=True)
    return out


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job 不存在")
        # 不返回完整 body（太大）
        short_articles = []
        for a in job["articles"]:
            short_articles.append({
                "seq": a.get("seq"),
                "topic": a.get("topic", ""),
                "title": a.get("title", ""),
                "body": a.get("body", "") + ("…" if len(a.get("body",""))>=200 else ""),
                "status": a.get("status", "pending"),
                "qr_url": a.get("qr_url"),
                "publish_id": a.get("publish_id"),
                "url": a.get("url", ""),
                "error": a.get("error"),
            })
        return {
            "id": job["id"], "topic": job["topic"], "topics_label": _topics_label(job.get("topics")),
            "phase": job["phase"],
            "articles": short_articles,
            "log": job["log"][-30:],
            "use_api": bool(job.get("use_api")),
            "mode_label": "API 全自动" if job.get("use_api") else "扫码发表",
        }


@app.get("/api/qr/{job_id}/{seq}")
async def get_qr(job_id: str, seq: int):
    qr_path = QR_DIR / f"{job_id}_{seq}.png"
    if not qr_path.exists():
        return JSONResponse({"wait": True, "msg": "二维码未生成或已被处理"}, status_code=202)
    return FileResponse(str(qr_path), media_type=_sniff_media(qr_path))


@app.get("/api/jobs/{job_id}/log/{seq}")
async def get_log(job_id: str, seq: int):
    log_path = LOG_DIR / f"{job_id}_{seq}.log"
    if not log_path.exists():
        return {"text": ""}
    return {"text": log_path.read_text(encoding="utf-8", errors="ignore")}


@app.post("/api/jobs/{job_id}/refresh_links")
async def refresh_job_links(job_id: str, request: Request, notify_again: bool = False):
    """对已完成的历史任务补抓文章永久链接（用 batchget 批量取，不耗 freepublish/get 配额）。

    用于：早期版本逐篇查链接耗尽日配额 / 或发布时未抓到链接的任务，事后一键补齐。
    可选 notify_again=true 时，补齐后重新推一条带链接的微信通知。
    """
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job 不存在")
        arts = job.get("articles", [])
        total = len([a for a in arts if a.get("publish_id")])
    _backfill_urls(job_id, rounds=1, gap=0)
    with jobs_lock:
        got = len([a for a in job.get("articles", []) if a.get("url")])
    if notify_again and notify.configured():
        try:
            notify.send_job_summary(job)
        except Exception as e:  # noqa: BLE001
            logger.warning("重发通知失败：%s", e)
    return {"ok": True, "published": total, "with_url": got}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str, request: Request):
    """用户手动取消正在进行的任务。仅置 cancel 标志，由 worker 在下一个安全检查点
    安全收尾（不会中断正在写入的磁盘/DB 操作），避免直接杀线程导致数据损坏。
    生成阶段取消：停止后续主题/篇的生成；发布阶段取消：保留已发布篇，剩余篇不再处理。"""
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(404, "job 不存在")
        phase = job.get("phase", "")
        if phase in ("done", "failed", "cancelled"):
            return {"ok": False, "msg": f"任务已处于终态（{phase}），无需取消"}
        if job.get("cancel"):
            return {"ok": True, "msg": "已在取消中"}
        job["cancel"] = True
        # 取消动作本身也是对外操作，留痕（含发起人）
        u = getattr(request.state, "user", None) or {}
        try:
            audit.audit(request, "job.cancel", f"取消任务（{job_id}，阶段 {phase}）",
                        user=u, result="ok")
        except Exception:  # noqa: BLE001
            pass
    logger.info("任务 %s 收到取消请求（阶段 %s）", job_id, phase)
    return {"ok": True, "msg": "已发送取消信号，worker 将在下一个安全检查点停止（正在进行的篇/发布不会中断）"}


def main():
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"\n  🚀 润致公众号自动发布工具 已启动 → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
