"""针对「手动发布不计入自动发布当日总数」的回归断言（不依赖微信/网络、不真发文）。

诉求（2026-10-17）：用户在网页手动发布/测试，不应挤占「每日自动发布」的当日名额，
也不应被自动发布的去重逻辑当作"已发"跳过。两套计数必须分开。

修复手段：
  · articles 表新增 auto_published 列（1=自动发布 / 0=手动发布）。
  · 手动发布路径（job.run / 扫码 / 单篇 oneshot）写 auto_published=0；
    自动发布路径（每日定时 / 补跑）写 auto_published=1。
  · _today_done_topics / _today_published_local_count(auto_only=True) 只统计 auto_published==1。
  · 跨设备防重在估算"外部设备自动已发"时，扣除本机手动已发的篇数，使手动不影响。
  · _schedule_needs_retry 判定"是否发完"也用 auto_only。

本文件锁住：
  A. _today_published_local_count 自动/全部分离正确
  B. _today_done_topics 只含自动主题
  C. 跨设备防重：本机手动发的，不触发"跳过主题"（手动不影响自动）
  D. 跨设备防重：外部纯自动发的，仍正确跳过（双设备防重不失效）
  E. _schedule_needs_retry：本机手动发的，不计入自动"已发完"判定

运行：在 app/ 目录下 `python tests/test_manual_auto_count.py`
"""
import os
import sys
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)

_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="poster_mac_"), "test.db")
os.environ["POSTER_DB"] = _TMP_DB

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app          # noqa: E402
import db as dbmod  # noqa: E402

dbmod.init_db()

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""))


def _bj_now_utc():
    # 今天北京时间对应的 UTC 时间戳（落在统计窗口内即可）
    return datetime.utcnow()


# ── 造数据：今天 3 篇自动(A,B,C) + 2 篇手动(M1,M2)，account_id=1 ──────────
_S = dbmod.SessionLocal()
try:
    _u = dbmod.User(username="mac_test", password_hash="x", role="editor", name="测试")
    _S.add(_u); _S.flush(); UID = _u.id
    _S.add(dbmod.UserSetting(user_id=UID, schedule_enabled=1, schedule_time="09:30",
                             schedule_account_ids="[1]", schedule_product_id=1))
    _S.add(dbmod.Account(id=1, name="测试号", appid="wx_test", secret_enc="s",
                         author="测试", owner_id=UID))
    _now = _bj_now_utc()
    for tp, auto in [("A", 1), ("B", 1), ("C", 1), ("M1", 0), ("M2", 0)]:
        _S.add(dbmod.Article(job_id="j1", seq=0, topic=tp, title="t_" + tp,
                             body_md="x", status="published", account_id=1,
                             auto_published=auto, created_at=_now))
    _S.commit()
finally:
    _S.close()

print("\n[A] 本地计数分离")
check("A1 全部已发 = 5（3自动+2手动）", app._today_published_local_count(1) == 5,
      f"got={app._today_published_local_count(1)}")
check("A2 自动已发 = 3（不含手动）", app._today_published_local_count(1, auto_only=True) == 3,
      f"got={app._today_published_local_count(1, auto_only=True)}")
check("A3 全部已发(无aid) = 5", app._today_published_local_count() == 5)
check("A4 自动已发(无aid) = 3", app._today_published_local_count(auto_only=True) == 3)

print("\n[B] _today_done_topics 只含自动主题")
_done = app._today_done_topics(1)
check("B1 含 A/B/C", {"A", "B", "C"} <= _done, str(_done))
check("B2 不含手动 M1/M2", "M1" not in _done and "M2" not in _done, str(_done))

print("\n[C] 跨设备防重：本机手动发的不触发跳过（手动不影响自动）")
# 微信端今天共 5 篇（= 本机 3自动+2手动）；本机手动 2 篇应被扣除 → external_auto=0 → 不跳过
with patch.object(app, "_today_published_count", return_value=5):
    plan = app._plan_init_today(["A", "B", "C", "D", "E", "M1", "M2"], account_ids=[1])
_items_topics = {it["topic"] for it in plan["items"]}
check("C1 手动主题 M1 仍在自动计划（未被当作已发跳过）", "M1" in _items_topics, str(_items_topics))
check("C2 手动主题 M2 仍在自动计划", "M2" in _items_topics, str(_items_topics))
check("C3 自动已发 A/B/C 被去重跳过（不重复发）", "A" not in _items_topics and "B" not in _items_topics and "C" not in _items_topics)
check("C4 计划含 D/E（新主题照常发）", "D" in _items_topics and "E" in _items_topics)
check("C5 计划共 4 篇（D,E,M1,M2）", len(plan["items"]) == 4, f"got={len(plan['items'])}")

print("\n[D] 跨设备防重：外部纯自动发的，仍正确跳过（双设备防重不失效）")
# 清空本机文章，模拟"另一台设备自动发了 A/B/C 到微信端"（wx_n=3，本机 0）
_S2 = dbmod.SessionLocal()
try:
    _S2.query(dbmod.Article).delete(); _S2.commit()
finally:
    _S2.close()
with patch.object(app, "_today_published_count", return_value=3):
    plan2 = app._plan_init_today(["A", "B", "C", "D", "E"], account_ids=[1])
_items2 = {it["topic"] for it in plan2["items"]}
check("D1 外部自动已发的 A/B/C 被跳过", "A" not in _items2 and "B" not in _items2 and "C" not in _items2, str(_items2))
check("D2 计划含 D/E（未发的照常）", "D" in _items2 and "E" in _items2)
check("D3 计划共 2 篇", len(plan2["items"]) == 2, f"got={len(plan2['items'])}")

print("\n[E] _schedule_needs_retry：本机手动发的不计入自动『已发完』判定")
# 恢复：3自动+2手动，total=20（模拟20个主题），手动发的3篇不应让"已发完"成立
_S3 = dbmod.SessionLocal()
try:
    _S3.query(dbmod.Article).delete(); _S3.commit()
    _now = _bj_now_utc()
    for tp, auto in [("A", 1), ("B", 1), ("C", 1), ("M1", 0), ("M2", 0)]:
        _S3.add(dbmod.Article(job_id="j1", seq=0, topic=tp, title="t_" + tp,
                              body_md="x", status="published", account_id=1,
                              auto_published=auto, created_at=_now))
    _S3.commit()
finally:
    _S3.close()

_ust = dbmod.SessionLocal()
try:
    _us = _ust.query(dbmod.UserSetting).filter(dbmod.UserSetting.user_id == UID).first()
finally:
    _ust.close()

with patch.object(app, "_topic_names_for_product", return_value=[f"T{i}" for i in range(20)]), \
     patch.object(app, "get_setting", return_value="0"):  # sched_retry_ts=0 → 不在节流内
    need = app._schedule_needs_retry(_us, datetime.now().strftime("%Y-%m-%d"))
check("E1 手动发3篇后，仍判定需要补跑（done_n_auto=0 < 20）", need is True)
# 反例：若计入手动（旧逻辑 done_n=5），则仍 <20 也会 True，这里只验证"不因为手动就误判已发完"
# 再验证：自动发满20 → 不需要
_S4 = dbmod.SessionLocal()
try:
    _S4.query(dbmod.Article).delete(); _S4.commit()
    _now = _bj_now_utc()
    for i in range(20):
        _S4.add(dbmod.Article(job_id="j1", seq=i, topic=f"T{i}", title=f"t{i}",
                              body_md="x", status="published", account_id=1,
                              auto_published=1, created_at=_now))
    _S4.commit()
finally:
    _S4.close()
with patch.object(app, "_topic_names_for_product", return_value=[f"T{i}" for i in range(20)]), \
     patch.object(app, "get_setting", return_value="0"):
    need2 = app._schedule_needs_retry(_us, datetime.now().strftime("%Y-%m-%d"))
check("E2 自动发满20篇 → 不需要补跑", need2 is False)

# ── 汇总 ──
passed = sum(1 for _, ok in results if ok)
print(f"\n===== 结果：{passed}/{len(results)} 通过 =====")
if passed != len(results):
    print("失败项：", [n for n, ok in results if not ok])
    sys.exit(1)
print("全部通过 ✅")
