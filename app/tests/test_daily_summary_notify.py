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
  —— 加固：完全跳过的日子（0 发布但有人因缺 Key 被硬拦）也要在合并汇总里被看到 ——
  10. 0 发布 / 有缺 Key 记录 → 仍发「未发布（缺 Key）」兜底提示（kind=keyfail）
  11. 有发布 / 有缺 Key 记录 → 发 digest 且 tail 含缺 Key 提示（合并摘要看得全）
  12. keyfail 已发过 / 后来有发布 → 升级发 digest（不漏「全部完成」）
  13. keyfail 已发过 / 仍 0 发布 → 不重复发

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


def _fake_get_setting(date_val="", kind_val="", sig_val=""):
    def _g(key, default=""):
        if key == "daily_summary_date":
            return date_val
        if key == "daily_summary_sent_kind":
            return kind_val
        if key == "daily_summary_sig":
            return sig_val
        return default
    return _g


def _run(force=False, configured=True, items=None, real_remaining=0, marker="", hour=18,
         batch_locked=False, key_failures=None, sent_kind="", sig_val=""):
    items = ITEM if items is None else items
    kf = key_failures if key_failures is not None else []
    fake_lock = MagicMock()
    fake_lock.locked.return_value = batch_locked
    with patch.object(app.notify, "configured", return_value=configured), \
         patch.object(app.notify, "send_daily_summary", return_value=[{"errcode": 0}]) as send_mock, \
         patch.object(app, "get_setting", side_effect=_fake_get_setting(marker, sent_kind, sig_val)) as get_mock, \
         patch.object(app, "set_setting") as set_mock, \
         patch.object(app, "_build_today_summary_items", return_value=items) as items_mock, \
         patch.object(app, "_real_pending_count", return_value=real_remaining), \
         patch.object(app, "_today_key_failures", return_value=kf), \
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
    check("③ 发送后写入 daily_summary_date / sent_kind=digest / sig 标记",
          set_.call_count == 3
          and ("daily_summary_date", TODAY) in [c.args for c in set_.call_args_list]
          and ("daily_summary_sent_kind", "digest") in [c.args for c in set_.call_args_list]
          and ("daily_summary_sig", "1") in [c.args for c in set_.call_args_list])

    # 4. 今天已对"同一批文章"发过（date==今日 且 sig 相同）→ 不重复发
    sent, send, get, set_, items = _run(marker=TODAY, sig_val="1", real_remaining=0)
    check("④ 今日已对同一批文章发过且未强制 → 不重复发", sent is False and send.call_count == 0)

    # 5. 强制重发（即使今日已发过）
    sent, send, get, set_, items = _run(marker=TODAY, sig_val="1", real_remaining=0, force=True)
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

    # 10. 0 发布 但 有缺 Key 记录 → 仍发「未发布（缺 Key）」兜底提示 + kind=keyfail
    kf = [{"user": "zjm", "err": "未设置你自己的 API Key（当前模型来源：deepseek）"}]
    sent, send, get, set_, items = _run(items=[], key_failures=kf)
    tail10 = send.call_args.args[2] if send.call_args else ""
    check("⑩ 0发布/有缺Key记录 → 仍发「未发布(缺Key)」", sent is True and send.call_count == 1)
    check("⑩ 内容含『缺少 API Key』与用户名 zjm",
          "缺少 API Key" in tail10 and "zjm" in tail10)
    check("⑩ 写 daily_summary_sent_kind=keyfail",
          ("daily_summary_sent_kind", "keyfail") in [c.args for c in set_.call_args_list])

    # 11. 有发布 且 有缺 Key 记录 → 发 digest 且 tail 含缺 Key 提示（合并摘要看得全）
    sent, send, get, set_, items = _run(real_remaining=0, key_failures=kf)
    tail11 = send.call_args.args[2] if send.call_args else ""
    check("⑪ 有发布/有缺Key → 发 digest 且 tail 含缺 Key 提示",
          sent is True and "zjm" in tail11 and "缺少 API Key" in tail11)

    # 12. keyfail 已发过 + 后来有发布 → 升级发 digest（不漏「全部完成」）
    sent, send, get, set_, items = _run(marker=TODAY, sent_kind="keyfail", real_remaining=0, key_failures=kf)
    check("⑫ keyfail已发/后补发布 → 升级发 digest", sent is True and send.call_count == 1)

    # 13. keyfail 已发过 + 仍0发布 → 不重复发
    sent, send, get, set_, items = _run(marker=TODAY, sent_kind="keyfail", items=[], key_failures=kf)
    check("⑬ keyfail已发/仍0发布 → 不重复发", sent is False and send.call_count == 0)

    # 14/15. _record_key_failure 写入 + _today_key_failures 读取 + 按用户去重
    store = {}
    def _set(k, v):
        store[k] = v
    def _get(k, d=""):
        return store.get(k, d)
    class _FakeUser:
        username = "zjm"
    class _FakeDB:
        def query(self, *a, **kw):
            return self
        def filter(self, *a, **kw):
            return self
        def first(self):
            return _FakeUser()
        def close(self):
            pass
    with patch.object(app, "get_setting", side_effect=_get), \
         patch.object(app, "set_setting", side_effect=_set), \
         patch.object(app, "SessionLocal", return_value=_FakeDB()):
        app._record_key_failure(2, "未设置你自己的 API Key")
        app._record_key_failure(2, "未设置你自己的 API Key")  # 重复应被去重
        kfs = app._today_key_failures()
    check("⑭ _record_key_failure 写入 + 按用户去重（同用户2次只留1）",
          len(kfs) == 1 and kfs[0].get("user") == "zjm")
    check("⑮ _today_key_failures 能读回当天记录", kfs[0].get("err", "").startswith("未设置你自己的"))

    ok = sum(1 for _, c in results if c)
    bad = len(results) - ok
    print("\n" + "=" * 60)
    print(f"=== 每日汇总触发门槛：通过 {ok} / 共 {len(results)}；失败 {bad} ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
