"""针对「原创 / 赞赏（打赏）」能力接线的单元断言（不依赖微信/网络、不真开浏览器）。

背景（微信硬限制）：官方 API(draft/add) 不支持设原创/赞赏，这两项**只能**由微信编辑器
UI（playwright 自动化）勾选。所以自动化发布想带上它们，必须把「建草稿」交给 playwright：
  填字段+封面+勾原创/赞赏 → 存草稿(save_draft_only)
  → find_draft_media_id_by_title → freepublish/submit（免扫码）
本文件验证这条链路的接线是通的：

  A. _ui_draft_for_original 决策矩阵：什么情况该走 UI 两段式、什么情况仍走纯 API
  B. publisher.publish_one 把 original/reward 原样透传给 node（含默认都开）
  C. RunRequest 的 original/reward 默认 True，且能接收 False
  D. publisher.js 里步骤3(原创)/步骤4(赞赏)确实被 WANT_ORIGINAL/WANT_REWARD 门控
     （防止后人误删门控、或误把步骤移出 gate 之外）

运行：在 app/ 目录下 `python tests/test_original_reward.py`
"""
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)

# ⚠️ import app 之前必须把 POSTER_DB 指到临时库，否则会在真实库上跑迁移/种子
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="poster_or_"), "test.db")
os.environ["POSTER_DB"] = _TMP_DB

if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import app          # noqa: E402
import publisher    # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond)))
    print(("✅ " if cond else "❌ ") + name + (f"   [{detail}]" if detail else ""))


def main():
    # ---- A. 决策矩阵：什么时候该走 UI 两段式 ----
    f = app._ui_draft_for_original
    A = [
        (("wx123", "wx123", True, True), True, "登录号 + 要原创赞赏 → 走 UI 两段式"),
        (("wx123", "wx123", True, False), True, "登录号 + 只要原创 → 走 UI"),
        (("wx123", "wx123", False, True), True, "登录号 + 只要赞赏 → 走 UI"),
        (("wx123", "wx123", False, False), False, "都不要 → 纯 API（没必要开浏览器）"),
        (("wx123", "other9", True, True), False, "非登录号 → 纯 API（微信限制，无法带）"),
        (("", "wx123", True, True), False, "未配登录号 → 纯 API"),
        (("", "", True, True), False, "都未配 → 纯 API"),
        ((" wx123 ", "wx123", True, True), True, "空白字符差异也能匹配"),
    ]
    for (args, exp, name) in A:
        got = f(*args)
        check(name, got == exp, f"→ {got}")

    # ---- B. publish_one 把开关原样透传给 node ----
    tmp = Path(tempfile.mkdtemp(prefix="poster_or_"))

    def _call(**kw):
        """调 publish_one 并抓到实际下发给 node 的命令行。"""
        with patch.object(publisher.subprocess, "run",
                          return_value=MagicMock(returncode=0, stdout="", stderr="")) as rm:
            publisher.publish_one(
                title="标题", body_md_path=tmp / "a.md", author="百秘一梳",
                cover_path=tmp / "c.png", qrcode_path=tmp / "q.png",
                log_path=tmp / "l.log", publish_id_path=tmp / "p.pid",
                save_draft_only=kw.get("save_draft_only", False),
                original=kw.get("original", True), reward=kw.get("reward", True))
            return rm.call_args[0][0]

    def _flag(cmd, key):
        return cmd[cmd.index(key) + 1] if key in cmd else None

    cmd_all = _call(save_draft_only=True, original=True, reward=True)
    check("B1 开原创+赞赏 → --original true / --reward true",
          _flag(cmd_all, "--original") == "true" and _flag(cmd_all, "--reward") == "true",
          f"{_flag(cmd_all, '--original')}/{_flag(cmd_all, '--reward')}")

    cmd_off = _call(save_draft_only=True, original=False, reward=False)
    check("B2 关两个 → --original false / --reward false",
          _flag(cmd_off, "--original") == "false" and _flag(cmd_off, "--reward") == "false",
          f"{_flag(cmd_off, '--original')}/{_flag(cmd_off, '--reward')}")

    cmd_mix = _call(save_draft_only=True, original=True, reward=False)
    check("B3 只要原创 → 原创 true / 赞赏 false（互不串）",
          _flag(cmd_mix, "--original") == "true" and _flag(cmd_mix, "--reward") == "false",
          f"{_flag(cmd_mix, '--original')}/{_flag(cmd_mix, '--reward')}")

    cmd_def = _call(save_draft_only=True)   # 不传 → 默认都开
    check("B4 不传开关 → 默认都开（保持历史行为：扫码模式本来就有原创+赞赏）",
          _flag(cmd_def, "--original") == "true" and _flag(cmd_def, "--reward") == "true")

    check("B5 save_draft_only=True → 下发 --save-draft-only true（存草稿而不是发表）",
          "true" == _flag(cmd_def, "--save-draft-only"))

    # ---- C. RunRequest 默认值与接收 ----
    r_def = app.RunRequest()
    check("C1 RunRequest 默认 original=True / reward=True",
          r_def.original is True and r_def.reward is True,
          f"original={r_def.original} reward={r_def.reward}")
    r_off = app.RunRequest(topics=[{"name": "t", "count": 1}], original=False, reward=False)
    check("C2 显式传 False 能被接收", r_off.original is False and r_off.reward is False)
    r_mix = app.RunRequest(topics=[{"name": "t", "count": 1}], original=True, reward=False)
    check("C3 原创/赞赏互相独立", r_mix.original is True and r_mix.reward is False)

    # ---- D. publisher.js 的步骤3/4 确实被门控（防误删） ----
    js = (Path(APP_DIR) / "publisher.js").read_text(encoding="utf-8")
    check("D1 publisher.js 定义了 WANT_ORIGINAL / WANT_REWARD",
          "WANT_ORIGINAL" in js and "WANT_REWARD" in js)
    # 步骤3（原创）必须在 if (WANT_ORIGINAL) 门控内
    m3 = re.search(r"=====\s*3\.\s*原创声明.*?if\s*\(\s*WANT_ORIGINAL\s*\)", js, re.S)
    check("D2 步骤3(原创声明) 处于 WANT_ORIGINAL 门控内", m3 is not None)
    m4 = re.search(r"=====\s*4\.\s*赞赏.*?if\s*\(\s*WANT_REWARD\s*\)", js, re.S)
    check("D3 步骤4(赞赏) 处于 WANT_REWARD 门控内", m4 is not None)
    # 存草稿必须在两步之后（否则原创/赞赏存不进草稿）
    i3 = js.find("===== 3. 原创声明")
    i4 = js.find("===== 4. 赞赏")
    isave = js.find("if (SAVE_DRAFT_ONLY)")
    check("D4 保存草稿在 原创(步骤3)/赞赏(步骤4) 之后 → 两者会存进草稿",
          i3 > 0 and i4 > i3 and isave > i4, f"3@{i3} 4@{i4} save@{isave}")
    # 步骤3/4 的点击动作必须在门控块内（而不是被留在块外无条件执行）
    body3 = js[js.find("if (WANT_ORIGINAL)"):i4]
    check("D5 原创的 group 点击在门控块内（关闭时不会误点原创）",
          "原创" in body3 and "appmsg-editor__setting-group" in body3)

    ok = sum(1 for _, c in results if c)
    bad = len(results) - ok
    print("\n" + "=" * 60)
    print(f"=== 原创/赞赏接线：通过 {ok} / 共 {len(results)}；失败 {bad} ===")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
