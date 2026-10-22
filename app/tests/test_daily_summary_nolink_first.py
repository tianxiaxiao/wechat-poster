"""针对「每日汇总去掉无链接首条」的回归断言（不依赖微信/网络）。

用户诉求（2026-10-19）：今天收到 4 条通知，预期 2 条。根因之一——
「全部完成」digest 在微信文章链接尚未就绪时先发了一条「点不开的汇总」，
等链接补全又由「自愈」补发一条带链接版，造成每日汇总发了两条。

本测试锁住（2026-10-19 修复后）：
  A. 「全部完成」(real_remaining==0) + 链接未就绪 + 白天(hour<23) + 未强制
     → 先不发声（不发出那条无链接首条），由调度循环(每20s)等链接就绪再发；
  B. 同一天、链接就绪后 → 只发 1 条「带链接」汇总（无链接首条从未发出）；
  C. 重复调用（链接就绪后）不再重发（一天最多一条带链接汇总）；
  D. 链接一直没就绪，但已等待超过 15 分钟（白天）→ 兜底发出（避免整晚静默，虽可能无链接）；
  E. 链接未就绪 + 已到深夜(hour>=23) → 兜底发出（避免整晚静默）。

运行：在 app/ 目录下 `python tests/test_daily_summary_nolink_first.py`
"""
import os
import sys
import time as _time

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from unittest.mock import patch, MagicMock

import app

TODAY = "2026-10-19"
ITEM_URL = [{"title": "A", "publish_id": "1", "url": "u_A", "theme": "t",
             "topic": "A", "status": "published"}]
ITEM_NO_URL = [{"title": "A", "publish_id": "1", "url": "", "theme": "t",
                "topic": "A", "status": "published"}]


def _fake_dt(hour: int):
    class _Now:
        @staticmethod
        def strftime(fmt):
            return TODAY
    class _DT:
        @classmethod
        def now(cls):
            n = _Now()
            n.hour = hour
            return n
    return _DT


# 持久 settings 存储（跨多次调用保持状态，模拟"同一天"）
_store = {}


def _get(k, d=""):
    return _store.get(k, d)


def _set(k, v):
    _store[k] = v


# 可控时钟（用于 15 分钟兜底判定）
_clock = [_time.time()]


def _run(hour=18, items=None, force=False, real_remaining=0, batch_locked=False):
    items = ITEM_URL if items is None else items
    fake_lock = MagicMock()
    fake_lock.locked.return_value = batch_locked
    sent_calls = []
    sm = MagicMock(side_effect=lambda *a, **k: sent_calls.append(a))
    with patch.object(app.notify, "configured", return_value=True), \
         patch.object(app.notify, "send_daily_summary", sm) as send_mock, \
         patch.object(app, "get_setting", side_effect=_get), \
         patch.object(app, "set_setting", side_effect=_set), \
         patch.object(app, "_build_today_summary_items", return_value=items), \
         patch.object(app, "_real_pending_count", return_value=real_remaining), \
         patch.object(app, "_today_key_failures", return_value=[]), \
         patch.object(app, "_batch_lock", fake_lock), \
         patch.object(app, "datetime", _fake_dt(hour)), \
         patch.object(app.time, "time", side_effect=lambda: _clock[0]):
        sent = app._ensure_daily_summary_sent(force=force)
    return sent, send_mock, sent_calls


def main():
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name + (f"  ({extra})" if extra else ""))

    # ── A. 全部完成 + 链接未就绪 + 白天 → 不发声（不发无链接首条）──
    _store.clear()
    _clock[0] = 1_000_000.0
    sent, sm, calls = _run(hour=18, items=ITEM_NO_URL, real_remaining=0)
    check("A 全部完成/链接未就绪/白天 → 暂不发（不发出无链接首条）",
          sent is False and sm.call_count == 0, f"calls={sm.call_count}")
    check("A 已写入等待计时(daily_summary_links_wait_since)",
          _store.get("daily_summary_links_wait_since", "") != "")

    # ── B. 同一天、链接就绪 → 只发 1 条带链接汇总（无链接首条从未发出）──
    _clock[0] = 1_000_060.0   # +60s，仍在 15 分钟窗口内
    sent, sm, calls = _run(hour=18, items=ITEM_URL, real_remaining=0)
    check("B 链接就绪后只发 1 条带链接汇总",
          sent is True and sm.call_count == 1, f"calls={sm.call_count}")
    items_arg = calls[0][1] if (calls and len(calls[0]) > 1) else []
    link_present = any((it.get("url") or "") for it in items_arg)
    check("B 发出的汇总 items 已带文章链接（非无链接首条）",
          link_present, str([it.get("url") for it in items_arg]))
    check("B 未先发过无链接版（本次调用只发 1 条）",
          sm.call_count == 1, f"calls={sm.call_count}")

    # ── C. 链接就绪后重复调用 → 不再重发（一天最多一条带链接）──
    sent, sm, calls = _run(hour=18, items=ITEM_URL, real_remaining=0)
    check("C 链接就绪后重复调用 → 不重发（一天最多一条带链接）",
          sent is False and sm.call_count == 0 and _store.get("daily_summary_date") == TODAY,
          f"sent={sent} calls={sm.call_count} date={_store.get('daily_summary_date')}")

    # ── D. 链接一直没就绪，已等待 >15 分钟（白天）→ 兜底发出 ──
    _store.clear()
    _clock[0] = 2_000_000.0
    sent, sm, calls = _run(hour=18, items=ITEM_NO_URL, real_remaining=0)
    check("D0 初始（链接未就绪/白天/刚等待）→ 暂不发",
          sent is False and sm.call_count == 0, f"calls={sm.call_count}")
    _clock[0] = 2_000_000.0 + 16 * 60   # +16 分钟，超过 15 分钟兜底阈值
    sent, sm, calls = _run(hour=18, items=ITEM_NO_URL, real_remaining=0)
    check("D 等待超 15 分钟仍无链接 → 兜底发出（避免整晚静默）",
          sent is True and sm.call_count == 1, f"calls={sm.call_count}")

    # ── E. 链接未就绪 + 深夜(hour>=23) → 兜底发出 ──
    _store.clear()
    _clock[0] = 3_000_000.0
    sent, sm, calls = _run(hour=23, items=ITEM_NO_URL, real_remaining=0)
    check("E 链接未就绪/深夜 → 兜底发出（避免整晚静默）",
          sent is True and sm.call_count == 1, f"calls={sm.call_count}")

    # ── 对照：force 应无视延迟直接发（即便链接未就绪，白天）──
    _store.clear()
    _clock[0] = 4_000_000.0
    sent, sm, calls = _run(hour=18, items=ITEM_NO_URL, real_remaining=0, force=True)
    check("F force=True → 无视延迟/链接直接发",
          sent is True and sm.call_count == 1, f"calls={sm.call_count}")

    ok = sum(1 for _, c in results if c)
    bad = len(results) - ok
    print("\n" + "=" * 60)
    print(f"=== 每日汇总·无链接首条修复：通过 {ok} / 共 {len(results)}；失败 {bad} ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
