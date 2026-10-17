"""针对「每日汇总只在全部完成后发一条」的回归断言（不依赖微信/网络）。

用户诉求（2026-10-17）：自动发布不要每发一篇就发通知，要全部发布完成后发一条；
手动发布(auto_published=0)不应污染这条自动汇总。

锁住：
  A. 批次运行中(剩余>0 且锁占用) → 不发；
  B. 全部完成(real_remaining==0) → 只发 1 条，调度循环每 20s 重复调用也不再发；
  C. 汇总只含自动发布(auto_published==1)的篇数，不含手动；
  D. 之后又发了新的自动文章(新的全部完成) → 再发 1 条，且篇数更新。

运行：在 app/ 目录下 `python tests/test_daily_summary_once.py`
"""
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
_TMP = os.path.join(tempfile.mkdtemp(prefix="poster_ds_"), "test.db")
os.environ["POSTER_DB"] = _TMP
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import db as dbmod  # noqa: E402
import app  # noqa: E402

dbmod.init_db()

# ── 造数据：今天 3 篇自动(A,B,C) + 2 篇手动(M1,M2)，account_id=1 ──
_S = dbmod.SessionLocal()
try:
    _S.add(dbmod.Account(id=1, name="测试号", appid="wx_t", secret_enc="s",
                         author="测试", owner_id=0))
    _now = datetime.utcnow()
    for tp, auto in [("A", 1), ("B", 1), ("C", 1), ("M1", 0), ("M2", 0)]:
        _S.add(dbmod.Article(job_id="j1", seq=0, topic=tp, title="t_" + tp,
                             body_md="x", status="published", account_id=1,
                             auto_published=auto, publish_id="pid_" + tp,
                             created_at=_now))
    _S.commit()
finally:
    _S.close()

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# 隔离的 settings 存储（避免污染测试库里的真实 settings 表）
_store = {}


def _get(k, d=None):
    return _store.get(k, d)


def _set(k, v):
    _store[k] = v


# 受控的"真实剩余篇数"
_rem = [0]

with patch.object(app, "get_setting", _get), \
     patch.object(app, "set_setting", _set), \
     patch.object(app, "_real_pending_count", lambda: _rem[0]), \
     patch.object(app, "_fetch_urls_with_retry", lambda pids: {}), \
     patch.object(app.notify, "configured", lambda: True):
    sent = []
    sm = MagicMock(side_effect=lambda *a, **k: sent.append(a))
    with patch.object(app.notify, "send_daily_summary", sm):

        # ── A. 批次运行中(锁占用 + 剩余>0) → 不发 ──
        _rem[0] = 5
        with app._batch_lock:   # 模拟批次正在跑，锁被占用
            app._ensure_daily_summary_sent()
        check("A 批次运行中(剩余5)不发送", len(sent) == 0)

        # ── B. 全部完成(real_remaining==0) → 只发 1 条；调度循环每 20s 重复调用不再发 ──
        _rem[0] = 0
        app._ensure_daily_summary_sent()
        app._ensure_daily_summary_sent()
        app._ensure_daily_summary_sent()
        check("B1 全部完成只发 1 条", len(sent) == 1, f"sent={len(sent)}")
        head = sent[0][0]
        check("B2 标题含『今日已发布 3 篇』(只算自动)", "今日已发布 3 篇" in head, head)
        check("B3 标题含『全部完成』", "全部完成" in head, head)

        # ── C. 之后又发了新的自动文章(新的全部完成) → 再发 1 条，篇数更新 ──
        _S2 = dbmod.SessionLocal()
        try:
            _now = datetime.utcnow()
            for tp in ("D", "E"):
                _S2.add(dbmod.Article(job_id="j2", seq=0, topic=tp, title="t_" + tp,
                                     body_md="x", status="published", account_id=1,
                                     auto_published=1, publish_id="pid_" + tp,
                                     created_at=_now))
            _S2.commit()
        finally:
            _S2.close()
        _rem[0] = 0
        app._ensure_daily_summary_sent()
        app._ensure_daily_summary_sent()
        check("C1 新完成(又发2篇自动)再发 1 条", len(sent) == 2, f"sent={len(sent)}")
        check("C2 第二条为 5 篇(3自动+2新自动)", "今日已发布 5 篇" in sent[1][0], sent[1][0])

        # ── D. 手动发布(auto_published=0)不应改变自动汇总集合 ──
        # 已发 5 篇自动并通知过；再发 2 篇手动，不应触发新汇总
        _S3 = dbmod.SessionLocal()
        try:
            _now = datetime.utcnow()
            for tp in ("M3", "M4"):
                _S3.add(dbmod.Article(job_id="j3", seq=0, topic=tp, title="t_" + tp,
                                     body_md="x", status="published", account_id=1,
                                     auto_published=0, publish_id="pid_" + tp,
                                     created_at=_now))
            _S3.commit()
        finally:
            _S3.close()
        _rem[0] = 0
        app._ensure_daily_summary_sent()
        check("D 手动发布后不触发新自动汇总", len(sent) == 2, f"sent={len(sent)}")

print("\n===== 结果：%d/%d 通过 =====" % (sum(1 for _, c in results if c), len(results)))
if not all(c for _, c in results):
    raise SystemExit(1)
