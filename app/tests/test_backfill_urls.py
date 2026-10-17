"""针对「手动发布通知带链接」的回归断言（不依赖微信/网络，全程 mock）。

诉求（2026-10-17）：手动发布完成通知里每篇应带微信文章永久链接，可点击查看。
之前缺链接有两层原因：
  1. 结构性：_backfill_urls 误用 fetch_recent_urls 的 by_mid 匹配，而项目实测 URL 内
     mid ≠ publish_id → by_mid 永远失配，只靠 by_title 标题精确匹配，覆盖不全；
  2. 配额：微信 freepublish/batchget、freepublish/get 当日配额耗尽(45009)时任何取链都失败
     （属平台限制，非代码 bug，午夜重置后恢复）。

本文件锁住结构性修复：_backfill_urls 必须
  · 先用 batchget 按【标题】匹配（bulk、省配额）；
  · 再用 publish_id 直取（_fetch_urls_with_retry）兜底；
  · 最终 job['articles'][*]['url'] 被填上（by_title 命中的 + publish_id 命中的）。

运行：在 app/ 目录下 `python tests/test_backfill_urls.py`
"""
import os
import sys
import tempfile
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="poster_bfu_"), "test.db")
os.environ["POSTER_DB"] = _TMP_DB

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app as appmod     # noqa: E402

dbmod = __import__("db")  # noqa: E402
dbmod.init_db()

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ── 造一个「手动发布」任务：3 篇有 publish_id、无 url；标题覆盖两种匹配 ──
_JOB_ID = "test_backfill_job"
_ARTICLES = [
    # by_title 能命中（标题与微信端一致）
    {"seq": 0, "topic": "A", "title": "降压药与梳子", "status": "published",
     "publish_id": "1001", "url": ""},
    # by_title 也命中
    {"seq": 1, "topic": "B", "title": "搬家塞把黑桃木梳", "status": "published",
     "publish_id": "1002", "url": ""},
    # by_title 故意不匹配（标题不一致），应走 publish_id 直取兜底
    {"seq": 2, "topic": "C", "title": "本地标题C", "status": "published",
     "publish_id": "1003", "url": ""},
]
with appmod.jobs_lock:
    appmod.jobs[_JOB_ID] = {"id": _JOB_ID, "phase": "done", "articles": _ARTICLES, "log": []}


def _fake_fetch_recent_urls(limit=40):
    # 微信端返回的标题映射（不含 publish_id=1003 对应的标题，模拟 by_title 失配）
    return {"by_mid": {"999999": "http://x/999"},  # by_mid 故意给错的，验证它不被采用
            "by_title": {"降压药与梳子": "https://mp.weixin.qq.com/s/AAA",
                         "搬家塞把黑桃木梳": "https://mp.weixin.qq.com/s/BBB"}}


def _fake_fetch_urls_with_retry(publish_ids, tries=5, wait=6):
    # 仅对未通过 by_title 命中的篇，按 publish_id 直取兜底
    return {"1003": "https://mp.weixin.qq.com/s/CCC"}


with patch.object(appmod.wechat_api, "fetch_recent_urls", _fake_fetch_recent_urls), \
     patch.object(appmod, "_fetch_urls_with_retry", _fake_fetch_urls_with_retry):
    appmod._backfill_urls(_JOB_ID)

with appmod.jobs_lock:
    got = {a["publish_id"]: a.get("url", "") for a in appmod.jobs[_JOB_ID]["articles"]}

print("\n[回填链接]")
check("by_title 命中 1001", got.get("1001") == "https://mp.weixin.qq.com/s/AAA", got.get("1001"))
check("by_title 命中 1002", got.get("1002") == "https://mp.weixin.qq.com/s/BBB", got.get("1002"))
check("publish_id 兜底命中 1003", got.get("1003") == "https://mp.weixin.qq.com/s/CCC", got.get("1003"))
check("by_mid 未被采用（无 '999999' 链接混入）",
      all("999999" not in (u or "") for u in got.values()))
check("回填日志已写", any("已回填" in (l or "") for l in appmod.jobs[_JOB_ID]["log"]),
      str(appmod.jobs[_JOB_ID]["log"]))

# 清理
with appmod.jobs_lock:
    appmod.jobs.pop(_JOB_ID, None)

passed = sum(1 for _, c in results if c)
total = len(results)
print(f"\n结果：{passed}/{total} 通过")
raise SystemExit(0 if passed == total else 1)
