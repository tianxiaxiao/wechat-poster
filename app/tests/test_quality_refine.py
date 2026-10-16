"""针对「质量精修 pass（去AI味 + 深度润色 + 可读性优化）」的单元断言（不依赖微信/网络）。

验证两件事：
  A. llm.refine_article 本身：成功解析 JSON 并采用；缺 body_md / 空稿 / 空 model 均抛异常
     （抛异常是为了让 _gen_with_dedup 回退原稿，绝不静默丢稿）。
  B. app._gen_with_dedup 的接入：quality_refine=True 时每篇定稿后调用精修并覆盖正文；
     quality_refine=False 时完全不调用（省时省钱）。

运行：在 app/ 目录下 `python tests/test_quality_refine.py`
（import app 需要从 app/ 目录解析 from db import ...）
"""
import os
import sys
from unittest.mock import patch, MagicMock

HERE = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.path.dirname(HERE)
if APP_DIR not in sys.path:
    sys.path.insert(0, APP_DIR)

import llm
import app


# ---- A. refine_article 本身 ----

class _FakeResp:
    def __init__(self, text):
        msg = type("M", (), {"content": text})()
        self.choices = [type("C", (), {"message": msg})()]


class _FakeClient:
    """模拟 openai.OpenAI 客户端：chat.completions.create 返回固定文本。"""
    def __init__(self, text):
        self._text = text
    def with_options(self, **kw):
        return self
    @property
    def chat(self):
        outer = self
        class _Comp:
            def create(self, **kw):
                return _FakeResp(outer._text)
        class _Chat:
            completions = _Comp()
        return _Chat()


def _patch_client(text):
    """把 _make_client 替换成返回固定响应的假客户端。"""
    return patch.object(llm, "_make_client", lambda *a, **k: _FakeClient(text))


def main():
    results = []

    def check(name, cond, extra=""):
        results.append((name, bool(cond)))
        print(("✅ " if cond else "❌ ") + name + (f"  ({extra})" if extra else ""))

    # A1. 正常：解析 JSON，title/body_md 被采用
    good = '{"title":"精修后的标题","body_md":"精修正文第一段\\n\\n精修正文第二段"}'
    with _patch_client(good):
        out = llm.refine_article("原标题", "原正文", provider="deepseek", model="deepseek-chat")
    check("A1 精修成功：title 被采用", out["title"] == "精修后的标题")
    check("A1 精修成功：body_md 被采用", "精修正文" in out["body_md"])

    # A2. 模型未指定 model → 回落到 provider 默认模型，仍成功（不抛异常）
    # 说明：所有已注册 provider 都有 default_model，所以空 model 不会触发 ValueError
    #       （函数里 `if not final_model` 仅为防御未知 provider，属死代码）。
    try:
        with _patch_client(good):
            out = llm.refine_article("t", "b", provider="deepseek", model="")
        check("A2 空 model → 回落 provider 默认模型并成功",
              out["body_md"] == "精修正文第一段\n\n精修正文第二段")
    except Exception as e:
        check("A2 空 model → 回落 provider 默认模型并成功", False, repr(e))

    # A3. 返回缺少 body_md → ValueError（驱动上层回退原稿）
    bad = '{"title":"只有标题","body_md":""}'
    try:
        with _patch_client(bad):
            llm.refine_article("t", "b", provider="deepseek", model="deepseek-chat")
        check("A3 缺 body_md → 抛 ValueError", False)
    except ValueError:
        check("A3 缺 body_md → 抛 ValueError", True)

    # A4. 空稿 → ValueError
    try:
        llm.refine_article("", "   ", provider="deepseek", model="deepseek-chat")
        check("A4 空稿 → 抛 ValueError", False)
    except ValueError:
        check("A4 空稿 → 抛 ValueError", True)

    # A5. 模型返回空正文 → ValueError（reasoning 模型未输出 content 的情形）
    try:
        with _patch_client("   "):
            llm.refine_article("t", "b", provider="deepseek", model="deepseek-chat")
        check("A5 返回空正文 → 抛 ValueError", False)
    except ValueError:
        check("A5 返回空正文 → 抛 ValueError", True)

    # ---- B. _gen_with_dedup 接入 ----

    _OK_DICT = {"ok": True, "title_sim": 0.0, "body_sim": 0.0,
                "matched_title": "", "matched_body_title": ""}

    def _run_dedup(quality_refine: bool):
        job = {
            "brand": "百秘一梳", "provider": "deepseek", "api_key": "",
            "base_url": "", "llm_model": "deepseek-chat",
            "product_name": "", "product_intro": "", "product_style": "",
            "allow_env_key": True, "quality_refine": quality_refine,
        }
        with patch.object(app, "_llm_wait_ready", return_value=True), \
             patch.object(app.dedup, "load_history", return_value=[]), \
             patch.object(app.dedup, "check_article", return_value=dict(_OK_DICT)), \
             patch.object(app.llm, "generate_articles",
                          return_value=[{"title": "T1", "body_md": "B1"}]), \
             patch.object(app.llm, "refine_article",
                          return_value={"title": "T1r", "body_md": "B1r"}) as refine_mock:
            out = app._gen_with_dedup("主题X", 1, job, [])
            return out, refine_mock

    # B1. 开启 → 调用精修且覆盖正文
    out, refine = _run_dedup(True)
    check("B1 quality_refine=True：精修被调用 1 次", refine.call_count == 1)
    check("B1 quality_refine=True：正文被精修结果覆盖", out and out[0]["body_md"] == "B1r")
    check("B1 quality_refine=True：标题被精修结果覆盖", out and out[0]["title"] == "T1r")

    # B2. 关闭 → 完全不调用，保留原稿
    out, refine = _run_dedup(False)
    check("B2 quality_refine=False：不调用精修", refine.call_count == 0)
    check("B2 quality_refine=False：保留原稿正文", out and out[0]["body_md"] == "B1")

    # B3. 开启但精修抛异常 → 保留原稿（不静默丢稿）
    job = {
        "brand": "百秘一梳", "provider": "deepseek", "api_key": "",
        "base_url": "", "llm_model": "deepseek-chat",
        "product_name": "", "product_intro": "", "product_style": "",
        "allow_env_key": True, "quality_refine": True,
    }
    with patch.object(app, "_llm_wait_ready", return_value=True), \
         patch.object(app.dedup, "load_history", return_value=[]), \
         patch.object(app.dedup, "check_article", return_value=dict(_OK_DICT)), \
         patch.object(app.llm, "generate_articles",
                      return_value=[{"title": "T1", "body_md": "B1"}]), \
         patch.object(app.llm, "refine_article", side_effect=RuntimeError("模型挂了")):
        out = app._gen_with_dedup("主题Y", 1, job, [])
    check("B3 精修抛异常 → 保留原稿正文", out and out[0]["body_md"] == "B1")

    ok = sum(1 for _, c in results if c)
    bad = len(results) - ok
    print(f"\n=== 质量精修 pass：通过 {ok} / 共 {len(results)}；失败 {bad} ===")
    if bad:
        for n, c in results:
            if not c:
                print("  失败项：", n)
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
