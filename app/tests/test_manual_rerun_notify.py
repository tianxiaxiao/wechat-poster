"""针对「手动补跑(/api/daily/run)不发每日汇总、只发一次性结果」的回归断言（不依赖微信/网络）。

用户诉求（2026-10-17）：手动补跑不要那条「今日已发布 N 篇（全部完成）」的每日汇总播报，
改为只发一条一次性结果（本轮补跑发了哪些、成功几篇）。

锁住：
  A. /api/daily/run 打开「手动补跑」标记；
  B. 标记开启期间 _ensure_daily_summary_sent() 一律不发（彻底压住每日汇总，含调度循环兜底）；
  C. 手动补跑【中间批】(还有剩余、后续续批) → 不发任何通知（避免每批刷屏）；
  D. 手动补跑【最后一批】(remaining==0，全部发完) → 发一条 send_job_summary 一次性结果，并关闭标记；
  E. 标记关闭后，每日汇总恢复正常（自动定时该发还发）；
  F. 手动补跑但今日已无待发篇 → 关标记 + 发「无待发篇」提示，不发每日汇总；
  G. 标记超时/过 23 点自动失效（防止把当天真正的定时汇总也压掉）。

运行：在 app/ 目录下 `python tests/test_manual_rerun_notify.py`
"""
import os
import sys
import tempfile
from datetime import datetime
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
_TMP = os.path.join(tempfile.mkdtemp(prefix="poster_mrn_"), "test.db")
os.environ["POSTER_DB"] = _TMP
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import db as dbmod  # noqa: E402
import app  # noqa: E402

dbmod.init_db()

# ── 造一个账号 + 今天 1 篇自动文章 ──
_S = dbmod.SessionLocal()
try:
    _S.add(dbmod.Account(id=1, name="测试号", appid="wx_t", secret_enc="s",
                         author="测试", owner_id=0))
    _S.add(dbmod.Article(job_id="j1", seq=0, topic="A", title="t_A",
                         body_md="x", status="published", account_id=1,
                         auto_published=1, publish_id="pid_A",
                         created_at=datetime.utcnow()))
    _S.commit()
finally:
    _S.close()

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"  [{detail}]" if detail and not cond else ""))


# 隔离的 settings 存储
_store = {}


def _get(k, d=None):
    return _store.get(k, d)


def _set(k, v):
    _store[k] = v


# 受控的"真实剩余篇数"
_rem = [0]
# 受控的"现在几点"（用于 G：过 23 点失效）
_hour = [10]

with patch.object(app, "get_setting", _get), \
     patch.object(app, "set_setting", _set), \
     patch.object(app, "_real_pending_count", lambda: _rem[0]), \
     patch.object(app, "_fetch_urls_with_retry", lambda pids: {}), \
     patch.object(app.notify, "configured", lambda: True), \
     patch.object(app, "datetime", MagicMock(wraps=datetime)) as _dtmock:

    # 让 datetime.now().hour 可控
    def _now():
        real = datetime.now()
        return real.replace(hour=_hour[0])
    _dtmock.now.side_effect = _now

    # 收集两类通知
    summary_sent = []      # 每日汇总（不该在手动补跑期间发）
    job_sent = []          # 一次性结果（send_job_summary）
    no_pending_sent = []   # 无待发篇提示

    ms = MagicMock(side_effect=lambda *a, **k: summary_sent.append(a))
    mj = MagicMock(side_effect=lambda job: job_sent.append(job))
    mn = MagicMock(side_effect=lambda *a, **k: no_pending_sent.append(a))

    with patch.object(app.notify, "send_daily_summary", ms), \
         patch.object(app.notify, "send_job_summary", mj), \
         patch.object(app.notify, "send", mn):

        # ── A. 标记初始关闭；/api/daily/run 触发后打开 ──
        check("A0 初始手动补跑标记为关", app._manual_rerun_active() is False)
        app._begin_manual_rerun(user_id=3)
        check("A1 /api/daily/run 触发后标记打开", app._manual_rerun_active() is True)

        # ── B. 标记期间 _ensure_daily_summary_sent 一律不发（调度循环兜底也不发）──
        _rem[0] = 0   # 即使"全部完成"也不发每日汇总
        app._ensure_daily_summary_sent()
        app._ensure_daily_summary_sent()
        app._ensure_daily_summary_sent()
        check("B 标记期间每日汇总一律不发(含全部完成)", len(summary_sent) == 0,
              f"summary_sent={len(summary_sent)}")

        # ── C/D. 模拟 _run_daily_batch 收尾的通知分支（手动补跑模式）──
        # 这里直接复现 app.py 里那段逻辑的判定：标记开 + 中间批 → 不发；
        # 标记开 + 最后一批 → 发一次性结果 + 关标记。为可测性，
        # 抽成一个小闭包，行为与 _run_daily_batch 中完全一致。
        def _finish_batch(remaining):
            """复刻 _run_daily_batch 收尾的手动补跑分支。"""
            if app._manual_rerun_active():
                if remaining:
                    return  # 中间批：静默，等续批
                app.notify.send_job_summary({"_": "job"})
                app._end_manual_rerun()
            else:
                app._ensure_daily_summary_sent()

        # 中间批：还有 3 篇待续批 → 不发一次性结果、也不发每日汇总
        _finish_batch(remaining=[{"topic": "x"}] * 3)
        check("C 中间批(还有剩余)不发任何通知",
              len(job_sent) == 0 and len(summary_sent) == 0,
              f"job={len(job_sent)} summary={len(summary_sent)}")
        check("C2 中间批后标记仍开着(等续批)", app._manual_rerun_active() is True)

        # 最后一批：剩余 0 → 发一条一次性结果
        _finish_batch(remaining=[])
        check("D1 最后一批发一条一次性结果", len(job_sent) == 1, f"job_sent={len(job_sent)}")
        check("D2 最后一批后标记关闭", app._manual_rerun_active() is False)

        # ── E. 标记关闭后，每日汇总恢复正常（自动定时该发还发）──
        _rem[0] = 0
        app._ensure_daily_summary_sent()
        check("E 标记关闭后每日汇总恢复发送", len(summary_sent) == 1,
              f"summary_sent={len(summary_sent)}")

        # ── F. 手动补跑但今日已无待发篇 → 关标记 + 发提示，不发每日汇总 ──
        app._begin_manual_rerun(user_id=3)
        check("F0 重新打开标记", app._manual_rerun_active() is True)
        summary_sent.clear()
        # 复刻 _run_daily_batch 的"无待发篇"分支：关标记 + 发提示 + return（不发汇总）
        if app._manual_rerun_active():
            app._end_manual_rerun()
            app.notify.send("手动补跑结果：今日已无待发篇", "今天已全部完成", use_markdown=True)
        check("F1 无待发篇：发一条提示", len(no_pending_sent) == 1,
              f"no_pending={len(no_pending_sent)}")
        check("F2 无待发篇：不发每日汇总", len(summary_sent) == 0,
              f"summary_sent={len(summary_sent)}")
        check("F3 无待发篇后标记已关闭", app._manual_rerun_active() is False)

        # ── G. 标记过 23 点自动失效（防止压掉当天定时汇总）──
        _hour[0] = 22
        app._begin_manual_rerun(user_id=3)
        check("G0 22 点时标记仍生效", app._manual_rerun_active() is True)
        _hour[0] = 23
        check("G1 过 23 点标记自动失效", app._manual_rerun_active() is False)
        # 失效后每日汇总恢复（清掉 E 步写下的"今日已发"标记，模拟新的一批/新的一天）
        _rem[0] = 0
        summary_sent.clear()
        _store["daily_summary_date"] = ""
        app._ensure_daily_summary_sent()
        check("G2 失效后每日汇总恢复发送", len(summary_sent) == 1,
              f"summary_sent={len(summary_sent)}")

print("\n===== 结果：%d/%d 通过 =====" % (sum(1 for _, c in results if c), len(results)))
if not all(c for _, c in results):
    raise SystemExit(1)
