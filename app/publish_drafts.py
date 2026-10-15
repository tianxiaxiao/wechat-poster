# -*- coding: utf-8 -*-
"""补发草稿箱中的待发布草稿（含 53501 频控退避 + 篇间间隔）。

用法:
  python publish_drafts.py            # 发布草稿箱全部草稿
  python publish_drafts.py 血压        # 只发布标题含"血压"的草稿（可多个关键词，任一命中即发）

说明:
  - 文章草稿已由 AI 生成好、只差 freepublish/submit 这一步（此前因 53501 频控失败残留）。
  - 发布成功的草稿会被微信自动移出草稿箱，脚本重跑不会重复发布。
"""
import sys
import time
import io

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import wechat_api as w  # noqa: E402  需在 app 目录下运行

KEYWORDS = [k for k in sys.argv[1:] if k]
DRAFT_GAP = 60            # 篇与篇之间间隔（秒）——实测微信提交间隔需≥约30分钟才稳定放行，这里 60s 起步
BACKOFF = [30, 60, 120, 180, 300]  # 53501 频控指数退避序列（秒）


def call(fn, n=4):
    """网络抖动重试封装。"""
    for i in range(n):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            print(f"  [网络] 尝试{i + 1}失败: {type(e).__name__} {str(e)[:50]}", flush=True)
            time.sleep(4)
    return {"errcode": -1, "errmsg": "network fail"}


def submit_with_backoff(mid, title):
    """提交发布单个草稿，遇 53501 按退避序列等待重试。"""
    for attempt in range(len(BACKOFF) + 1):
        d = call(lambda: w._api("freepublish/submit", {"media_id": mid}))
        code = d.get("errcode", 0)
        if code == 0:
            return d
        if code == 53501 and attempt < len(BACKOFF):
            wait = BACKOFF[attempt]
            print(f"  [53501 频控] 等待 {wait}s 后重试 ({attempt + 1}/{len(BACKOFF) + 1}) | {title[:30]}", flush=True)
            time.sleep(wait)
            continue
        return d
    return {"errcode": -1, "errmsg": "retry exhausted"}


def main():
    d = call(lambda: w._api("draft/batchget", {"offset": 0, "count": 50, "no_content": 1}))
    items = d.get("item", [])
    if not items:
        print("草稿箱为空，无待补发草稿。", flush=True)
        return

    targets = []
    for it in items:
        try:
            t = it["content"]["news_item"][0].get("title", "")
        except Exception:  # noqa: BLE001
            t = "?"
        if not KEYWORDS or any(k in t for k in KEYWORDS):
            targets.append((it["media_id"], t))

    print(f"草稿箱共 {len(items)} 篇，命中待补发 {len(targets)} 篇。", flush=True)
    if not targets:
        print("没有命中关键词的草稿，本次不发布任何文章。", flush=True)
        return

    ok = 0
    for i, (mid, title) in enumerate(targets):
        r = submit_with_backoff(mid, title)
        if r.get("errcode", 0) == 0:
            ok += 1
            print(f"  OK 第{i + 1}/{len(targets)} 发布成功 publish_id={r.get('publish_id')} | {title[:35]}", flush=True)
        else:
            print(f"  FAIL 第{i + 1}/{len(targets)} errcode={r.get('errcode')} {str(r.get('errmsg', ''))[:40]} | {title[:35]}", flush=True)
        if i < len(targets) - 1:
            time.sleep(DRAFT_GAP)

    print(f"补发完成: 成功 {ok}/{len(targets)}。", flush=True)
    # 统计剩余草稿（发布成功会被微信自动移出草稿箱）
    d2 = call(lambda: w._api("draft/batchget", {"offset": 0, "count": 50, "no_content": 1}))
    print(f"草稿箱剩余: {len(d2.get('item', []))} 篇", flush=True)


if __name__ == "__main__":
    main()
