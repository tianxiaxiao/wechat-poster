"""针对「每日汇总与批次解耦 + 调度循环兜底」的单元断言（不依赖微信/网络）。

验证 _ensure_daily_summary_sent 的触发门槛（剩余判断基于 DB 真实发布数 _real_pending_count，
而非陈旧的计划文件 _today_pending_count；并区分"批次是否在跑"）：
  1. 未配置群机器人 → 不发
  2. 今天还没发过任何文章 → 不发
  3. 已发布 + 今天未发过 + 真实剩余==0（含手动补发已发完）→ 发「全部完成」+ 写标记
  4. 今天已发过（标记==今日）且未强制 → 不重复发
  5. 强制(force) → 无视标记重发
  6. 有剩余 / 白天 / 批次在跑 / 未强制 → 暂不发（等发完/深夜）
  7. 有剩余 / 白天 / 无批次在跑 / 未强制 → 发「今日还剩 N 篇」（手动补发完也能白天收到）
  8. 有剩余 / 深夜 → 仍发收尾汇总
  9. 有剩余 / 白天 / 批次在跑 / force → 发（强制无视批次）

运行：在 app/ 目录下 `python tests/test_daily_summary_notify.py`
（import app 需要从 app/ 目录解析 from db import ...）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

from unittest.mock import patch, MagicMock

import app

TODAY = "2026-10-16"
ITEM = [{"title": "A", "publish_id": "1", "url": "u", "theme": "t", "topic": "A", "status": "published"}]


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


def _run(force=False, configured=True, items=None, real_remaining=0, marker="", hour=18,
         batch_locked=False):
    items = ITEM if items is None else items
    fake_lock = MagicMock()
    fake_lock.locked.return_value = batch_locked
    with patch.object(app.notify, "configured", return_value=configured), \
         patch.object(app.notify, "send_daily_summary", return_value=[{"errcode": 0}]) as send_mock, \
         patch.object(app, "get_setting", return_value=marker) as get_mock, \
         patch.object(app, "set_setting") as set_mock, \
         patch.object(app, "_build_today_summary_items", return_value=items) as items_mock, \
         patch.object(app, "_real_pending_count", return_value=real_remaining), \
         patch.object(app, "_batch_lock", fake_lock), \
         patch.object(app, "datetime", _fake_dt(hour)):
        sent = app._ensure_daily_summary_sent(force=force)
    return sent, send_mock, get_mock, set_mock, items_mock


def main():
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name + (f"  ({extra})" if extra else ""))

    # 1. 未配置机器人
    sent, send, get, set_, items = _run(configured=False)
    check("① 未配置群机器人 → 不发且 send 不调用", sent is False and send.call_count == 0)

    # 2. 没有已发布文章
    sent, send, get, set_, items = _run(items=[])
    check("② 今天无已发布文章 → 不发", sent is False and send.call_count == 0)

    # 3. 正常：已发布 + 未发过 + 真实剩余==0（含手动补发已发完）→ 发 + 写标记
    sent, send, get, set_, items = _run(marker="", real_remaining=0)
    check("③ 已发布/未发过/真实剩余0 → 发送「全部完成」", sent is True and send.call_count == 1)
    check("③ 发送后写入 daily_summary_date 标记",
          set_.call_count == 1 and set_.call_args == (("daily_summary_date", TODAY),))

    # 4. 今天已发过（marker==今日）未强制 → 不重复发
    sent, send, get, set_, items = _run(marker=TODAY, real_remaining=0)
    check("④ 今日已发过且未强制 → 不重复发", sent is False and send.call_count == 0)

    # 5. 强制重发（即使今日已发过）
    sent, send, get, set_, items = _run(marker=TODAY, real_remaining=0, force=True)
    check("⑤ force=True → 无视标记重发", sent is True and send.call_count == 1)

    # 6. 有剩余 / 白天 / 批次在跑 / 未强制 → 暂不发（等发完/深夜）
    sent, send, get, set_, items = _run(marker="", real_remaining=5, hour=18, batch_locked=True)
    check("⑥ 有剩余/白天/批次在跑/未强制 → 暂不发", sent is False and send.call_count == 0)

    # 7. 有剩余 / 白天 / 无批次在跑 / 未强制 → 发「今日还剩 N 篇」（手动补发完也能白天收到）
    sent, send, get, set_, items = _run(marker="", real_remaining=5, hour=18, batch_locked=False)
    check("⑦ 有剩余/白天/无批次/未强制 → 仍发「还剩 N 篇」", sent is True and send.call_count == 1)

    # 8. 有剩余 / 深夜 → 仍发收尾汇总（无论批次）
    sent, send, get, set_, items = _run(marker="", real_remaining=5, hour=23, batch_locked=False)
    check("⑧ 有剩余/深夜 → 仍发收尾汇总", sent is True and send.call_count == 1)

    # 9. 有剩余 / 白天 / 批次在跑 / force → 发（强制无视批次）
    sent, send, get, set_, items = _run(marker="", real_remaining=5, hour=18, batch_locked=True, force=True)
    check("⑨ 有剩余/白天/批次在跑/force → 发", sent is True and send.call_count == 1)

    ok = sum(1 for _, c in results if c)
    bad = len(results) - ok
    print("\n" + "=" * 60)
    print(f"=== 每日汇总触发门槛：通过 {ok} / 共 {len(results)}；失败 {bad} ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
