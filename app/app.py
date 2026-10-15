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
                User, AuditLog)
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
        ok, err = llm.ping(provider, api_key=job.get("api_key", ""),
                           base_url=job.get("base_url", ""), model=job.get("llm_model", ""))
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
        time.sleep(wait)


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
        "phase": "queued",     # queued / generating / publishing_p1 / .../ done / failed
        "articles": [],        # [{seq, topic, title, status, qr_url, ok, error, publish_id}]
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
            job["phase"] = "done"
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


@app.get("/api/users")
async def api_users(request: Request):
    auth.need_role(request, "admin")
    db = SessionLocal()
    try:
        return [auth.public_user(u) for u in db.query(User).order_by(User.id).all()]
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
        audit.audit(request, "user.create",
                    f"新增用户 {u.username}（{u.name}，角色 {auth.ROLES.get(u.role)}，"
                    f"{'启用' if u.is_active else '停用'}）", user=me)
        return {"ok": True, "user": auth.public_user(u)}
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
        db.commit()
        audit.audit(request, "user.update", f"修改用户 {u.username}：{'；'.join(changes) or '无变化'}",
                    user=me, level="warn" if "重置密码" in changes or "停用" in changes else "info")
        return {"ok": True, "user": auth.public_user(u)}
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
@app.get("/api/products")
async def list_products_api():
    db = SessionLocal()
    try:
        return [{"id": p.id, "name": p.name, "intro": p.intro, "style": p.style} for p in db.query(Product).order_by(Product.id).all()]
    finally:
        db.close()


@app.post("/api/products")
async def add_product_api(p: ProductIn, request: Request):
    me = auth.need_role(request, "editor")
    if not p.name.strip():
        raise HTTPException(400, "产品名不能为空")
    db = SessionLocal()
    try:
        prod = Product(name=p.name.strip(), intro=p.intro.strip(), style=p.style.strip())
        db.add(prod); db.commit(); db.refresh(prod)
        audit.audit(request, "product.create", f"新增产品「{prod.name}」", user=me)
        return {"id": prod.id, "name": prod.name}
    finally:
        db.close()


@app.put("/api/products/{pid}")
async def edit_product_api(pid: int, p: ProductIn, request: Request):
    me = auth.need_role(request, "editor")
    if not p.name.strip():
        raise HTTPException(400, "产品名不能为空")
    db = SessionLocal()
    try:
        prod = db.query(Product).filter(Product.id == pid).first()
        if not prod:
            raise HTTPException(404, "产品不存在")
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
    db = SessionLocal()
    try:
        prod = db.query(Product).filter(Product.id == pid).first()
        if prod:
            nm = prod.name
            db.delete(prod); db.commit()
            audit.audit(request, "product.delete", f"删除产品「{nm}」", user=me, level="warn")
        return {"ok": True}
    finally:
        db.close()


# ========== 主题库 CRUD ==========
@app.get("/api/topics")
async def list_topics_api():
    db = SessionLocal()
    try:
        return [{"id": t.id, "name": t.name, "desc": t.desc} for t in db.query(Topic).order_by(Topic.id).all()]
    finally:
        db.close()


class TopicIn(BaseModel):
    name: str
    desc: str = ""


@app.post("/api/topics")
async def add_topic_api(t: TopicIn, request: Request):
    me = auth.need_role(request, "editor")
    if not t.name.strip():
        raise HTTPException(400, "主题名不能为空")
    db = SessionLocal()
    try:
        if db.query(Topic).filter(Topic.name == t.name.strip()).first():
            raise HTTPException(400, "主题已存在")
        tp = Topic(name=t.name.strip(), desc=t.desc.strip())
        db.add(tp); db.commit(); db.refresh(tp)
        audit.audit(request, "topic.create", f"新增主题「{tp.name}」", user=me)
        return {"id": tp.id, "name": tp.name}
    finally:
        db.close()


@app.put("/api/topics/{tid}")
async def edit_topic_api(tid: int, t: TopicIn, request: Request):
    me = auth.need_role(request, "editor")
    if not t.name.strip():
        raise HTTPException(400, "主题名不能为空")
    db = SessionLocal()
    try:
        tp = db.query(Topic).filter(Topic.id == tid).first()
        if not tp:
            raise HTTPException(404, "主题不存在")
        dup = db.query(Topic).filter(Topic.name == t.name.strip(), Topic.id != tid).first()
        if dup:
            raise HTTPException(400, "已有同名主题")
        tp.name = t.name.strip()
        tp.desc = t.desc.strip()
        db.commit()
        audit.audit(request, "topic.update", f"修改主题「{tp.name}」", user=me)
        return {"ok": True, "id": tp.id, "name": tp.name}
    finally:
        db.close()


@app.delete("/api/topics/{tid}")
async def del_topic_api(tid: int, request: Request):
    me = auth.need_role(request, "editor")
    db = SessionLocal()
    try:
        tp = db.query(Topic).filter(Topic.id == tid).first()
        if tp:
            nm = tp.name
            db.delete(tp); db.commit()
            audit.audit(request, "topic.delete", f"删除主题「{nm}」", user=me, level="warn")
        return {"ok": True}
    finally:
        db.close()


# ========== 公众号账号 CRUD ==========
@app.get("/api/accounts")
async def list_accounts_api(request: Request):
    """账号列表：AppSecret 只对管理员返回，其余角色脱敏（公网部署避免密钥泄露）"""
    u = auth.need_login(request)
    is_admin = u["role"] == "admin"
    db = SessionLocal()
    try:
        out = []
        for a in db.query(Account).order_by(Account.id).all():
            sec = a.secret_enc or ""
            out.append({"id": a.id, "name": a.name, "appid": a.appid,
                        "secret": sec if is_admin else (sec[:4] + "****" if sec else ""),
                        "secret_masked": not is_admin, "author": a.author})
        return out
    finally:
        db.close()


@app.post("/api/accounts")
async def add_account_api(a: AccountIn, request: Request):
    me = auth.need_role(request, "admin")     # 含 AppSecret，仅管理员可改
    if not a.name.strip() or not a.appid.strip() or not a.secret.strip():
        raise HTTPException(400, "账号名/appid/secret 不能为空")
    db = SessionLocal()
    try:
        exists = db.query(Account).filter(Account.appid == a.appid.strip()).first()
        if exists:
            raise HTTPException(400, f"已存在 appid={a.appid.strip()} 的公众号")
        acct = Account(name=a.name.strip(), appid=a.appid.strip(),
                       secret_enc=a.secret.strip(), author=a.author.strip() or "百秘一梳")
        db.add(acct); db.commit(); db.refresh(acct)
        audit.audit(request, "account.create", f"新增公众号「{acct.name}」(appid={acct.appid})",
                    user=me, level="warn")
        return {"id": acct.id, "name": acct.name}
    finally:
        db.close()


@app.delete("/api/accounts/{aid}")
async def del_account_api(aid: int, request: Request):
    me = auth.need_role(request, "admin")
    db = SessionLocal()
    try:
        acct = db.query(Account).filter(Account.id == aid).first()
        if acct:
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
    product_name, product_intro, product_style = "", "", ""
    if req.product_id:
        prod = db.get(Product, req.product_id)
        if prod:
            product_name, product_intro, product_style = prod.name, prod.intro, (prod.style or "")
    account = None
    if req.account_id:
        account = db.get(Account, req.account_id)
    db.close()

    brand = product_name or req.brand
    job = _new_job(tl, brand, req.author, req.extra_hint, operator=operator)
    job["provider"] = req.provider
    job["api_key"] = req.api_key
    job["llm_model"] = req.model or llm.PROVIDERS[req.provider]["default_model"]
    job["base_url"] = req.base_url or llm.PROVIDERS[req.provider]["default_base_url"]
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


@app.get("/api/schedule")
async def api_schedule_get():
    """返回当前定时配置 + 下次触发时间文案"""
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
    · 否则立即在后台跑当天批次（服务常驻进程里执行，不受外部会话影响）"""
    me = auth.need_role(request, "editor")
    today = datetime.now().strftime("%Y-%m-%d")
    plan = _plan_load()
    if plan.get("date") == today:
        items = plan.get("items", [])
        pending = [i for i in items if i.get("status") != "done"]
        if items and not pending:
            raise HTTPException(400, f"今日批次已完成（{len(items)} 篇），无需补跑")
    SCHED_CFG["last_run_date"] = today
    _save_sched()
    threading.Thread(target=_run_daily_batch, args=("手动补跑",), daemon=True).start()
    logger.info("手动触发每日批次（补跑）")
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
BATCH_GAP_MIN = 15        # 批间休息（分钟）；仅当一天发不完时才用到（撞限后按 35/70/120 递增）
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


def _today_done_topics() -> set:
    """今天（北京时间）已成功发布的主题集合，避免续批/重启后重复发布。"""
    bj_now = datetime.utcnow() + timedelta(hours=8)
    bj_day_start = bj_now.replace(hour=0, minute=0, second=0, microsecond=0)
    utc_start = bj_day_start - timedelta(hours=8)
    db = SessionLocal()
    try:
        rows = db.query(Article).filter(Article.created_at >= utc_start, Article.status == "published").all()
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


def _today_published_local_count() -> int:
    """本机数据库记录的"今天（北京时间）已发布"篇数（用于与微信端对比，发现外部发布）。"""
    bj_now = datetime.utcnow() + timedelta(hours=8)
    utc_start = bj_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    db = SessionLocal()
    try:
        return db.query(Article).filter(Article.created_at >= utc_start,
                                        Article.status == "published").count()
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


def _plan_init_today(topics: list) -> dict:
    done = _today_done_topics()
    # ── 跨设备防重 ───────────────────────────────────────────────
    # 若微信端今日已发篇数 > 本机数据库记录的今日已发篇数，说明另有设备（或手动入口）发过。
    # 两台设备主题库与顺序一致，故把"多出来的篇数"按顺序视为已发，避免重复发文。
    wx_n = _today_published_count()
    local_n = _today_published_local_count()
    if wx_n > local_n:
        extra = min(wx_n - local_n, len(topics))
        if extra > 0:
            skipped = list(topics[:extra])
            done = done | set(skipped)
            logger.warning("跨设备防重：微信端今日已发 %d 篇、本机记录 %d 篇 → 跳过前 %d 个主题：%s",
                           wx_n, local_n, extra, "、".join(skipped))
    items = []
    # ── 已生成文章队列优先：先发库存，发完再实时生成 ──────────────
    queue = _load_gen_queue()
    if queue:
        taken, rest = 0, []
        for it in queue:
            tp = (it.get("topic") or "").strip()
            if taken < len(topics) and tp and tp not in done:
                items.append({"topic": tp, "title": it.get("title") or "",
                              "body_path": it.get("body_path") or "",
                              "draft_mid": "", "status": "pending"})
                taken += 1
            else:
                rest.append(it)
        _save_gen_queue(rest)
        if taken:
            logger.info("从已生成队列取 %d 篇今日发布（队列剩余 %d 篇）", taken, len(rest))
    # 队列不够 20 篇时，剩余主题仍走即时生成（发完库存自动切回生成）
    covered = {it["topic"] for it in items}
    items += [{"topic": nm, "title": "", "draft_mid": "", "status": "pending"}
              for nm in topics if nm not in done and nm not in covered]
    return {"date": datetime.now().strftime("%Y-%m-%d"), "items": items, "next_ts": 0, "batch_done": 0}


def _run_daily_batch(reason: str = "每日定时"):
    """执行一个批次：发布计划队列前 BATCH_SIZE 篇（有草稿的直接补发，否则生成→存草稿→提交）。
    结束后：还有剩余 → 30 分钟后自动续批；全部完成 → 清计划。结果推送到微信群。"""
    if not _batch_lock.acquire(blocking=False):
        logger.info("已有批次在执行，跳过本次触发")
        return
    try:
        _ensure_card_render_ok("每日批次开始")   # 渲染管线自检：不可用立即告警，避免静默降级
        db = SessionLocal()
        try:
            topics = [t.name for t in db.query(Topic).order_by(Topic.id).all()]
            account = db.query(Account).order_by(Account.id).first()
            product = db.query(Product).order_by(Product.id).first()
        finally:
            db.close()
        if not topics:
            logger.warning("定时发布跳过：主题库为空")
            return

        plan = _plan_load()
        if plan.get("date") != datetime.now().strftime("%Y-%m-%d"):
            # 新的一天：重建计划，但保留昨天尚未补发完的手动发布篇（有正文/草稿的），避免丢失
            leftover = [it for it in plan.get("items", [])
                        if it.get("status") != "done" and (it.get("body_path") or it.get("draft_mid"))]
            plan = _plan_init_today(topics)
            if leftover:
                plan["items"] = leftover + plan["items"]
                logger.info("跨天保留未补发的 %d 篇（有正文/草稿）", len(leftover))
        plan["items"] = [it for it in plan.get("items", []) if it.get("topic") in topics or it.get("draft_mid") or it.get("body_path")]
        pending = [it for it in plan["items"] if it.get("status") != "done"]
        if not pending:
            logger.info("今日发布计划已全部完成")
            plan["next_ts"] = 0
            _plan_save(plan)
            return
        # ── 跨设备双保险 ─────────────────────────────────────────
        # 若微信端今日已发篇数已达到/超过今日计划总数，说明另一台设备已完成今天的发布，
        # 本批直接跳过（避免两台同时开定时造成重复发文）。
        wx_n = _today_published_count()
        if wx_n > 0 and wx_n >= len(plan["items"]):
            logger.warning("跨设备防重：微信端今日已发 %d 篇 ≥ 今日计划 %d 篇，本批跳过（另一台设备可能已发完）",
                           wx_n, len(plan["items"]))
            plan["next_ts"] = 0
            _plan_save(plan)
            return

        batch = pending[:BATCH_SIZE]
        batch_no = int(plan.get("batch_done", 0)) + 1
        job_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
        job = {
            "id": job_id,
            "topics": [{"name": it["topic"], "count": 1} for it in batch],
            "topic": batch[0]["topic"] if batch else "",
            "total": len(batch), "count": len(batch),
            "brand": product.name if product else "百秘一梳",
            "author": (account.author if account else "百秘一梳"),
            "extra_hint": "",
            "created_at": datetime.now().isoformat(),
            "phase": "generating",
            "articles": [],
            "log": [f"[{datetime.now().isoformat()}] ⏰ {reason}：第 {batch_no} 批开始（本批 {len(batch)} 篇，计划剩余 {len(pending)} 篇）"
                    + ("" if notify.configured() else "｜⚠️ 未配置 ROBOT_WEBHOOK_URL，完成通知将跳过")],
            "provider": "deepseek", "api_key": "",
            "llm_model": llm.PROVIDERS["deepseek"]["default_model"],
            "base_url": llm.PROVIDERS["deepseek"]["default_base_url"],
            "use_api": True,
            "product_name": product.name if product else "",
            "product_intro": product.intro if product else "",
            "product_style": (product.style or "") if product else "",
            "account_appid": account.appid if account else "",
            "account_secret": account.secret_enc if account else "",
            "account_name": account.name if account else "默认",
            "source_label": "每日定时", "notify_on_done": True,
        }
        with jobs_lock:
            jobs[job_id] = job
        if account:
            try:
                wechat_api.set_account(account.appid, account.secret_enc)
            except Exception as e:
                job["log"].append(f"⚠️ 切换公众号失败：{e}")

        ok = 0
        hit_limit = False
        ip_blocked = False   # 40164：本机公网 IP 不在公众号白名单（需人工去后台加白）
        for idx, it in enumerate(batch):
            topic = it["topic"]
            seq = idx
            with jobs_lock:
                job["phase"] = f"publishing_p{seq+1}"
                job["log"].append(f"[{datetime.now().isoformat()}] 开始第 {seq+1}/{len(batch)} 篇：{topic}"
                                  + ("（草稿已存在，直接补发）" if it.get("draft_mid") else ""))
            job["articles"].append({"seq": seq, "topic": topic,
                                    "title": it.get("title") or f"{topic}（自动生成）", "status": "publishing"})
            try:
                mid = it.get("draft_mid") or ""
                title = it.get("title") or ""
                if not mid:
                    body = ""
                    # 优先复用本地已生成的正文（手动发布未成功篇的补发，不重复消耗 LLM 生成）
                    bp = it.get("body_path") or ""
                    if bp:
                        f = ARTICLES_DIR / Path(bp).name
                        if f.exists():
                            body = f.read_text(encoding="utf-8")
                    if not body:
                        items = _gen_with_dedup(topic, 1, job, job["log"])
                        if not items:
                            raise RuntimeError("LLM 未返回文章")
                        title = items[0]["title"]; body = items[0]["body_md"]
                        md_p = ARTICLES_DIR / f"{job_id}_{seq}.md"
                        md_p.write_text(body, encoding="utf-8")
                        # 立即把标题/正文路径写入计划：后续批次失败重试时直接复用，不再重复消耗 LLM 生成
                        it["title"] = title
                        it["body_path"] = md_p.name
                    title = title or f"{topic}"
                    # md2card 公众号模式卡片排版；自动发布随机选 4 个主题之一（不含日本杂志），失败降级旧版
                    body_html, _tmid, _tmname = wxcard.render_md_with_theme_safe(body)
                    job["log"].append(f"  第 {seq+1} 篇：排版主题「{_tmname}」")
                    it["theme"] = _tmname        # 记进计划，供「每天一条」汇总显示排版主题
                    # 存草稿（封面裁剪失败 53402 属随机封面偶发问题 → 换一张封面立即重试，避免白等一批）
                    mid, _cover_note = _add_draft_with_cover_retry(
                        title=title, body_html=body_html, author=job["author"],
                        digest=(body or "")[:100])
                    if _cover_note:
                        job["log"].append(f"  {_cover_note}")
                    it["title"] = title; it["draft_mid"] = mid
                    job["articles"][seq]["title"] = title
                    try:
                        db2 = SessionLocal()
                        db2.add(Article(job_id=job_id, seq=seq, topic=topic, title=title, body_md=body, status="generated"))
                        db2.commit(); db2.close()
                    except Exception:
                        pass
                job["log"].append(f"  第 {seq+1} 篇：提交发布（草稿 {str(mid)[:12]}…）")
                pub = wechat_api.publish_draft(mid, fast_fail=True)   # 批次内快速失败，失败篇留待下一批
                pid = str(pub.get("publish_id") or "")
                url = ""   # 链接统一在批次结束后用一次 batchget 回填（逐篇查询会耗尽 freepublish/get 日配额）
                it["status"] = "done"
                job["articles"][seq]["status"] = "published"
                job["articles"][seq]["publish_id"] = pid
                if url:
                    job["articles"][seq]["url"] = url
                    it["url"] = url
                # 落库发布结果：既让网页/统计准确，也让 _today_done_topics（本机今日已发）
                # 与微信端统计保持一致 —— 跨设备防重依赖这个数据
                try:
                    db3 = SessionLocal()
                    row = db3.query(Article).filter(Article.job_id == job_id, Article.seq == seq).first()
                    if row:
                        row.status = "published"
                        row.publish_id = pid
                    else:
                        db3.add(Article(job_id=job_id, seq=seq, topic=topic,
                                        title=it.get("title") or "", body_md="",
                                        status="published", publish_id=pid))
                    db3.commit()
                    db3.close()
                except Exception as _e:
                    logger.warning("批次发布结果落库失败(不影响发布)：%s", str(_e)[:100])
                ok += 1
                job["log"].append(f"✅ 第 {seq+1} 篇发布成功 publish_id={pid}" + (f"\n   链接: {url}" if url else ""))
            except Exception as e:
                it["status"] = "failed"       # 保留 draft_mid，下组直接补发
                job["articles"][seq]["status"] = "failed"
                job["articles"][seq]["error"] = str(e)[:200]
                job["log"].append(f"❌ 第 {seq+1} 篇失败（留待下一批补发）：{str(e)[:120]}")
                if "40164" in str(e):
                    ip_blocked = True
                if "53501" in str(e):
                    hit_limit = True
                    job["log"].append("  ⚠️ 命中微信频率限制 → 本批提前结束，剩余篇留到下一批（避免继续撞限加重冷却）")
                    break
            if idx < len(batch) - 1:
                time.sleep(POST_INTERVAL_SEC)   # 组内篇间隔，避免密集提交触发惩罚性限流

        remaining = [it for it in plan["items"] if it.get("status") != "done"]
        plan["batch_done"] = batch_no
        # 连续全失败时逐次拉长批间隔（30→60→120→180 分钟封顶），避免无效高频重试
        if ok > 0:
            plan["fail_streak"] = 0
        else:
            plan["fail_streak"] = int(plan.get("fail_streak", 0)) + 1
        if remaining and datetime.now().hour < 23:
            fs = int(plan.get("fail_streak", 0))
            # 实测该账号撞限后约每 30 分钟才放行 1 次：全成功批→休 60min；刚撞限→休 35min，连撞则 70/120 递增
            gap_min = BATCH_GAP_MIN if fs < 1 else (35 if fs == 1 else (70 if fs == 2 else 120))
            gap_min = min(gap_min, 180)
            plan["next_ts"] = int(time.time()) + gap_min * 60
        else:
            plan["next_ts"] = 0
        _plan_save(plan)
        nxt_txt = datetime.fromtimestamp(plan["next_ts"]).strftime("%H:%M") if remaining else ""
        with jobs_lock:
            job["phase"] = "done"
            job["log"].append(f"本批完成：成功 {ok}/{len(batch)}；今日剩余 {len(remaining)} 篇"
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
                                    f"已发 {ok} 篇，剩余 {len(remaining)} 篇将在 {nxt_txt} 自动重试（无需处理）。",
                                    use_markdown=True)
                    elif datetime.now().hour >= 23:
                        r = _build_today_summary_items()
                        if r:
                            notify.send_daily_summary(
                                f"## 📰 润致发布 · 今日收尾：已发 {len(r)} 篇",
                                r, f"⏳ 今日还剩 {len(remaining)} 篇，将于明天继续")
        except Exception as e:
            logger.warning("批次通知失败：%s", e)
        logger.info("批次完成 job=%s 成功=%d/%d 剩余=%d", job_id, ok, len(batch), len(remaining))
        audit.audit(None, "publish.done",
                    f"每日批次 {job_id}（{reason}）完成：成功 {ok}/{len(batch)} 篇，今日剩余 {len(remaining)} 篇",
                    user={"username": (job.get("operator") or {}).get("username") or ("系统" if reason != "手动补跑" else "手动补跑"),
                          "role": (job.get("operator") or {}).get("role", "")},
                    level="warn" if ok < len(batch) else "info")
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
                  "publish_id": str(r.publish_id or ""), "status": "published"} for r in rows]
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
        it["theme"] = theme_by_title.get(it["title"], "")
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


def _scheduler_loop():
    """后台调度线程（每 20s）：
    1) 每日定时：到达设定时刻（起 3 分钟窗口内）→ 启动当天第 1 批；
       窗口错过也补跑——只要当天还没触发过、且当前时间仍在 CATCHUP_UNTIL_HOUR 之前，
       就补跑一次（服务晚启动/重启后不会整天漏发，2026-09-29 新增）；
    2) 续批：发布计划里有 next_ts 且到点 → 自动续下一批（用分批避开微信频率风控）。"""
    while True:
        try:
            _ip_watchdog()
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            # 1) 每日定时起点（含错过后补跑）
            if SCHED_CFG.get("enabled"):
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
                            logger.info("触发每日批次：%s", reason)
                            threading.Thread(target=_run_daily_batch, args=(reason,), daemon=True).start()
                        else:
                            logger.warning("今日定时已错过且已过 %d 点，不再补跑（避免深夜发文）", CATCHUP_UNTIL_HOUR)
                            SCHED_CFG["last_run_date"] = today      # 标记已处理，避免反复判断
                            _save_sched()
                except Exception as e:
                    logger.warning("定时检查异常: %s", e)
            # 2) 续批
            plan = _plan_load()
            nxt = int(plan.get("next_ts") or 0)
            if nxt and plan.get("date") == today and time.time() >= nxt:
                plan["next_ts"] = 0      # 先清零防重复触发；批次结束会重新写
                _plan_save(plan)
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


def main():
    import uvicorn
    host = os.getenv("HOST", "127.0.0.1")
    port = int(os.getenv("PORT", "8765"))
    print(f"\n  🚀 润致公众号自动发布工具 已启动 → http://{host}:{port}\n")
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
