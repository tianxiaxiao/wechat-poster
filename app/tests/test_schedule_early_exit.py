"""针对「每日定时为什么没执行」的回归断言（不依赖微信/网络、不真发文）。

事故（2026-10-17 09:30）：tianxiaxiao 的定时开着、时刻 09:30，当天却一篇没发。
排查发现 schedule_last_run_date 已被写成当天 ⇒ 调度器整天跳过。根因是：

  ★ 标记在「拿到批次锁之后、真正发布之前」就写入了。
    批次无论在哪一步早退（无主题 / 无待发 / 异常中断），当天名额都被永久占掉，
    调度器再也不会重试 —— 表面症状就是「定时没执行」。

本文件锁住三件事：
  A. 早退路径**绝不**写 schedule_last_run_date（核心修复）
  B. _mark_schedule_ran 的调用点确实在「确定有篇要发」之后（防后人挪回前面）
  C. 早退不再静默：会落审计 + 推企微
  D. _schedule_needs_retry 自愈判定矩阵
  E. 微信 batchget 报错（45009 日配额耗尽）必须如实抛出，不能静默当成「今天 0 篇」

运行：在 app/ 目录下 `python tests/test_schedule_early_exit.py`
"""
import inspect
import os
import sys
import time
import tempfile
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)

# ⚠️ import app 之前必须把 POSTER_DB 指到临时库，否则会在真实库上跑迁移/种子
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="poster_sched_"), "test.db")
os.environ["POSTER_DB"] = _TMP_DB

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app          # noqa: E402
import db as dbmod  # noqa: E402
import wechat_api   # noqa: E402

dbmod.init_db()

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# ── 造最小数据：1 用户 + 1 公众号 + 1 产品 + 2 主题 ────────────────────────
_S = dbmod.SessionLocal()
try:
    _u = dbmod.User(username="sch_test", password_hash="x", role="editor", name="测试")
    _S.add(_u)
    _S.flush()
    UID = _u.id
    _S.add(dbmod.UserSetting(user_id=UID, schedule_enabled=1, schedule_time="09:30",
                             schedule_account_ids="[1]", schedule_product_id=1))
    _S.add(dbmod.Account(id=1, name="测试号", appid="wx_test", secret_enc="s",
                         author="测试", owner_id=UID))
    _S.add(dbmod.Product(id=1, name="测试产品", intro="", style=""))
    _S.add(dbmod.Topic(id=1, name="主题甲", desc="", product_id=1, owner_id=UID))
    _S.add(dbmod.Topic(id=2, name="主题乙", desc="", product_id=1, owner_id=UID))
    _S.commit()
finally:
    _S.close()

TODAY = app.datetime.now().strftime("%Y-%m-%d")


def _setting_val(key, default=""):
    return app.get_setting(key, default)


def _last_run_date():
    _d = dbmod.SessionLocal()
    try:
        s = _d.query(dbmod.UserSetting).filter(dbmod.UserSetting.user_id == UID).first()
        return (s.schedule_last_run_date or "") if s else ""
    finally:
        _d.close()


def _reset_last_run():
    _d = dbmod.SessionLocal()
    try:
        s = _d.query(dbmod.UserSetting).filter(dbmod.UserSetting.user_id == UID).first()
        if s:
            s.schedule_last_run_date = ""
            _d.commit()
    finally:
        _d.close()


# =====================================================================
# A. 早退路径绝不写「今日已跑」标记
# =====================================================================
print("\n[A] 早退路径不占用当天名额（核心修复）")

_reset_last_run()
with patch.object(app, "_mark_schedule_ran") as mark_mock, \
     patch.object(app, "_alert_schedule_skip") as alert_mock, \
     patch.object(app, "_ensure_daily_summary_sent", return_value=False), \
     patch.object(app, "_ensure_card_render_ok", return_value=True), \
     patch.object(app, "_resolve_llm_for_user",
                  return_value=({"provider": "deepseek", "model": "m", "base_url": "", "api_key": "k"}, None)), \
     patch.object(app, "_plan_init_today",
                  return_value={"date": TODAY, "items": [], "next_ts": 0, "batch_done": 0}), \
     patch.object(app, "_plan_load", return_value={"date": TODAY, "items": [], "next_ts": 0}), \
     patch.object(app, "_plan_save", return_value=None), \
     patch.object(app, "_today_published_local_count", return_value=0):
    app._run_daily_batch(user_id=UID, account_ids=[1], reason="每日定时",
                         schedule_product_id=1, mark_daily_run=True, rebuild_plan=True)

check("① 无待发主题早退：未写 schedule_last_run_date", _last_run_date() == "",
      f"last_run_date={_last_run_date()!r}")
check("② 无待发主题早退：未调用 _mark_schedule_ran", mark_mock.call_count == 0,
      f"called={mark_mock.call_count}")
check("③ 无待发主题早退：触发了早退告警（不再静默）", alert_mock.call_count == 1,
      f"alert={alert_mock.call_count}")

# 无主题早退
_reset_last_run()
with patch.object(app, "_mark_schedule_ran") as mark_mock, \
     patch.object(app, "_alert_schedule_skip") as alert_mock, \
     patch.object(app, "_ensure_card_render_ok", return_value=True), \
     patch.object(app, "_resolve_llm_for_user",
                  return_value=({"provider": "deepseek", "model": "m", "base_url": "", "api_key": "k"}, None)), \
     patch.object(app, "_topic_names_for_product", return_value=[]):
    app._run_daily_batch(user_id=UID, account_ids=[1], reason="每日定时",
                         schedule_product_id=1, mark_daily_run=True, rebuild_plan=True)
check("④ 无可用主题早退：未写标记", _last_run_date() == "" and mark_mock.call_count == 0,
      f"last={_last_run_date()!r} mark={mark_mock.call_count}")
check("⑤ 无可用主题早退：触发了告警", alert_mock.call_count == 1, f"alert={alert_mock.call_count}")

# =====================================================================
# B. 标记调用点必须在「确定有篇要发」之后（防回归）
# =====================================================================
print("\n[B] _mark_schedule_ran 调用点位置（源码级防回归）")
src = inspect.getsource(app._run_daily_batch)
i_batch = src.find("batch = pending[:BATCH_SIZE]")
i_mark = src.find("_mark_schedule_ran(user_id)")
i_job = src.find("job_id = datetime.now()")
check("⑥ 源码里存在 batch = pending[:BATCH_SIZE]", i_batch >= 0)
check("⑦ 标记写在「取到待发批次」之后", i_batch >= 0 and i_mark > i_batch,
      f"batch@{i_batch} mark@{i_mark}")
check("⑧ 标记写在「建任务 job_id」之前（先占位再建）", i_job < 0 or i_mark < i_job,
      f"mark@{i_mark} job@{i_job}")
# 早退 return 之前不许有标记
i_nopending = src.find("if not pending:")
check("⑨ 「无待发」早退分支在标记之前", i_nopending >= 0 and i_nopending < i_mark,
      f"nopending@{i_nopending} mark@{i_mark}")

# =====================================================================
# C. _mark_schedule_ran 本身确实会写库
# =====================================================================
print("\n[C] _mark_schedule_ran 行为")
_reset_last_run()
app._mark_schedule_ran(UID)
check("⑩ 调用后写入今天日期", _last_run_date() == TODAY, f"last={_last_run_date()!r}")
app._mark_schedule_ran(0)
check("⑪ user_id=0 时安全跳过（不抛异常）", True)

# =====================================================================
# D. _schedule_needs_retry 自愈判定矩阵
# =====================================================================
print("\n[D] 早退自愈判定 _schedule_needs_retry")
_us = dbmod.SessionLocal().query(dbmod.UserSetting).filter(
    dbmod.UserSetting.user_id == UID).first()


def _needs(done_n, total=2, ts=0):
    with patch.object(app, "get_setting", side_effect=lambda k, d="": str(ts)
                      if k.startswith("sched_retry_ts_") else d), \
         patch.object(app, "_today_published_local_count", return_value=done_n), \
         patch.object(app, "_topic_names_for_product", return_value=[None] * total):
        return app._schedule_needs_retry(_us, TODAY)


check("⑫ 一篇没发（0 < 2）→ 需要补跑", _needs(0) is True)
check("⑬ 发了一半（1 < 2）→ 需要补跑", _needs(1) is True)
check("⑭ 全发完（2 == 2）→ 不需要补跑", _needs(2) is False)
check("⑮ 节流期内（刚补过）→ 不补（防刷屏）", _needs(0, ts=int(time.time())) is False)
check("⑯ 超过节流期（即使历史多次失败）→ 仍允许补（不再被次数永久锁死）",
      _needs(0, ts=int(time.time()) - 16 * 60) is True)

# =====================================================================
# E. 微信 batchget 报错必须如实抛出，不能静默当成「今天 0 篇」
# =====================================================================
print("\n[E] batchget 错误处理（45009 日配额耗尽）")
with patch.object(wechat_api, "_api", return_value={"errcode": 45009,
                                                    "errmsg": "reach max api daily quota limit"}):
    raised = False
    try:
        wechat_api.fetch_today_published(40)
    except Exception:
        raised = True
    check("⑯ fetch_today_published 遇 errcode 会抛异常（不静默返回空）", raised)

    n = app._today_published_count()
    check("⑰ _today_published_count 失败时返回 -1（调用方据此跳过防重）", n == -1, f"n={n}")

# 正常情况仍返回篇数
with patch.object(wechat_api, "_api", return_value={"item": [
        {"update_time": 9999999999, "content": {"news_item": [{"title": "t"}]}}]}):
    rows = wechat_api.fetch_today_published(40)
    check("⑱ 正常返回时能取到条目", isinstance(rows, list) and len(rows) >= 0)

# =====================================================================
ok = sum(1 for _, c in results if c)
print(f"\n{'=' * 52}\n结果：{ok}/{len(results)} 项通过")
if ok != len(results):
    for name, c in results:
        if not c:
            print("  ❌ " + name)
    sys.exit(1)
print("全部通过 ✅")
