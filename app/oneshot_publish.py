"""一次性「生成 N 篇 + 指定主题发布」（免扫码 API 路径）
================================================================
用法：
    python oneshot_publish.py [数量=4] [主题逗号列表=bytedance,alibaba,notebook,business]

行为：
  · 按主题库顺序取 N 个「今天还没发过」的主题，每个主题生成 1 篇（走统一的历史去重逻辑）
  · 每篇分别用指定主题渲染 md2card 公众号卡片排版后发布（draft/add → freepublish/submit）
  · 篇间隔 POST_INTERVAL_SEC 秒（微信频控保护），命中 53501 立即停止剩余
  · 结果写入 articles 表（与每日定时同一套落库逻辑，统计/去重/防重都能看到）
"""
import sys
import time
import datetime
from pathlib import Path

import app as A
import llm
import notify
import wechat_api
import wxcard
from db import SessionLocal, Article, Account, Product, Topic

APP_DIR = Path(__file__).parent
LOGS = []


def log(msg: str):
    line = f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}"
    LOGS.append(line)
    print(line, flush=True)


def notify_summary(results: list, ok: int, total: int):
    """推送企业微信通知（与每日定时批次同款：带文章链接，超长自动分片）"""
    try:
        if not notify.configured():
            log("（未配置 ROBOT_WEBHOOK_URL，跳过通知）")
            return
        urls = {}
        try:
            # 带重试取链接：刚发布的文章微信要几秒到几十秒才收录（曾导致末篇没链接）
            urls = A._fetch_urls_with_retry([r.get("publish_id") for r in results])
        except Exception as e:
            log(f"取链接失败（将不带链接）：{str(e)[:80]}")
        head = f"## 📰 润致发布·手动发布完成：成功 {ok}/{total} 篇"
        lines = []
        for r in results:
            mk = "✅" if r["ok"] else "❌"
            t = (r["title"] or r["topic"] or "").strip()[:50]
            u = notify.safe_article_url(urls.get(str(r.get("publish_id")), ""))
            lines.append(f"- {mk} [{t}]({u})" if u else f"- {mk} {t}")
        notify.send_markdown_chunks(head, lines)
        log("已推送企业微信通知")
    except Exception as e:
        log(f"通知推送失败：{str(e)[:150]}")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    if len(sys.argv) > 2:
        # 显式指定主题 → 按顺序轮换（篇数多于主题数时循环使用）
        base = [wxcard.resolve_theme(t) for t in sys.argv[2].split(",")]
        themes = [base[i % len(base)] for i in range(n)]
    else:
        # 未指定 → 每篇在 4 个主题里随机（与每日自动发布一致）
        themes = [wxcard.pick_theme() for _ in range(n)]

    db = SessionLocal()
    try:
        account = db.query(Account).order_by(Account.id).first()
        product = db.query(Product).order_by(Product.id).first()
        all_topics = [t.name for t in db.query(Topic).order_by(Topic.id).all()]
    finally:
        db.close()
    if not all_topics:
        log("❌ 主题库为空")
        return

    done = A._today_done_topics()
    picked = [t for t in all_topics if t not in done][:n]
    if len(picked) < n:
        picked = all_topics[:n]

    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_") + "oneshot"
    job = {
        "id": job_id, "brand": product.name if product else "百秘一梳",
        "author": account.author if account else "百秘一梳",
        "extra_hint": "",
        "provider": "deepseek",
        "api_key": "", "llm_model": llm.PROVIDERS["deepseek"]["default_model"],
        "base_url": llm.PROVIDERS["deepseek"]["default_base_url"],
        "product_name": product.name if product else "",
        "product_intro": product.intro if product else "",
        "product_style": (product.style or "") if product else "",
    }
    if account:
        wechat_api.set_account(account.appid, account.secret_enc)

    log(f"任务 {job_id}：{n} 篇，主题 {'、'.join(picked)}；排版主题 {'、'.join(wxcard.THEMES[t] for t in themes)}")

    ok = 0
    results = []
    for i, (topic, theme) in enumerate(zip(picked, themes)):
        seq = i
        log(f"—— 第 {i+1}/{len(picked)} 篇：主题『{topic}』 ——")
        rec = {"topic": topic, "title": "", "ok": False}
        results.append(rec)
        try:
            items = A._gen_with_dedup(topic, 1, job, LOGS)
            if not items:
                raise RuntimeError("LLM 未返回文章")
            title, body = items[0]["title"], items[0]["body_md"]
            md_path = A.ARTICLES_DIR / f"{job_id}_{seq}.md"
            md_path.write_text(body, encoding="utf-8")
            log(f"   标题：{title}")

            body_html, tid, tname = wxcard.render_md_with_theme_safe(body, theme)
            log(f"   排版主题：{tname}（{tid}）")
            thumb = wechat_api.pick_random_cover_media()
            mid = wechat_api.add_draft(title=title, content_html=body_html,
                                      author=account.author if account else "百秘一梳",
                                      thumb_media_id=thumb, digest=(body or "")[:100])
            log(f"   草稿已建 {str(mid)[:16]}…，提交发布…")
            pub = wechat_api.publish_draft(mid, fast_fail=True)
            pid = str(pub.get("publish_id") or "")

            db = SessionLocal()
            try:
                db.add(Article(job_id=job_id, seq=seq, topic=topic, title=title,
                               body_md=body, status="published", publish_id=pid))
                db.commit()
            finally:
                db.close()
            ok += 1
            rec["ok"] = True
            rec["title"] = title
            rec["theme"] = tname
            rec["publish_id"] = pid
            log(f"   ✅ 发布成功 publish_id={pid}")
        except Exception as e:
            log(f"   ❌ 失败：{str(e)[:200]}")
            if "53501" in str(e):
                log("   命中微信频率限制，停止剩余发布")
                break

        if i < len(picked) - 1:
            log(f"   等待 {A.POST_INTERVAL_SEC}s（微信频控保护）…")
            time.sleep(A.POST_INTERVAL_SEC)

    log(f"===== 完成：成功 {ok}/{len(picked)} 篇 =====")
    notify_summary(results, ok, len(picked))


if __name__ == "__main__":
    main()
