"""
test_local_llm_one_by_one.py — 本地/自建模型「逐篇生成」验证
================================================================
背景：本地/自建模型（qwen.dadaobo.com 网关 / 局域网 llama.cpp / Ollama）单篇就要十几秒起，
输出 token 也受限。一次要 2~3 篇极易超 max_tokens 被截断、JSON 不完整，甚至直接超时
（表现为「生成不出」）。所以这类模型必须【每批只生成 1 篇、逐篇串行】。

本脚本断言（打桩计数，不只看代码）：
  · is_local_llm 判定：ollama / openai_compat / 内网 IP / 自建域名 → True；公网云端 → False
  · plan_batches 分批：本地 count=3 →[1,1,1]；云端 count=3 →[3]；云端 count=5 →[3,2]
  · 【关键】实际调用次数：本地 provider 时 generate_articles(count=3) 真的发起 3 次
    单篇调用（每次 count=1）；云端仍是 1 次 3 篇（原行为不变）
运行：python tests/test_local_llm_one_by_one.py
"""
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

import llm  # noqa: E402

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


def main():
    print("\n[1] is_local_llm 判定（谁该逐篇）")
    check("ollama → 本地", llm.is_local_llm("ollama", "") is True)
    check("openai_compat → 本地", llm.is_local_llm("openai_compat", "") is True)
    check("deepseek 公网 → 非本地",
          llm.is_local_llm("deepseek", "https://api.deepseek.com/v1") is False,
          str(llm.is_local_llm("deepseek", "https://api.deepseek.com/v1")))
    check("qwen.dadaobo.com（自建域名）→ 本地",
          llm.is_local_llm("deepseek", "https://qwen.dadaobo.com/v1") is True)
    check("局域网 192.168.x → 本地",
          llm.is_local_llm("deepseek", "http://192.168.123.5:8000/v1") is True)
    check("localhost → 本地",
          llm.is_local_llm("deepseek", "http://localhost:11434/v1") is True)
    check("内网 10.x → 本地", llm.is_local_llm("x", "http://10.0.0.7:8000/v1") is True)
    check("内网 172.16.x → 本地", llm.is_local_llm("x", "http://172.16.5.9:8000/v1") is True)
    check("172.32.x 不属于内网 → 非本地",
          llm.is_local_llm("x", "http://172.32.1.1:8000/v1") is False)
    check("空 base_url + 未知 provider → 安全判为非本地",
          llm.is_local_llm("", "") is False)

    print("\n[2] plan_batches 分批规划")
    check("本地 count=3 → [1,1,1]（逐篇）", llm.plan_batches(3, "ollama") == [1, 1, 1],
          str(llm.plan_batches(3, "ollama")))
    check("本地 count=1 → [1]", llm.plan_batches(1, "openai_compat") == [1],
          str(llm.plan_batches(1, "openai_compat")))
    check("云端 count=3 → [3]（一次3篇，原行为）", llm.plan_batches(3, "deepseek") == [3],
          str(llm.plan_batches(3, "deepseek")))
    check("云端 count=5 → [3,2]", llm.plan_batches(5, "deepseek") == [3, 2],
          str(llm.plan_batches(5, "deepseek")))
    check("兜底：deepseek provider 但 base_url 指向 127.0.0.1 → 逐篇",
          llm.plan_batches(3, "deepseek", "http://127.0.0.1:8000/v1") == [1, 1, 1])

    print("\n[3] 【关键】真实调用次数（打桩计数）")
    calls = []

    def fake_gen_batch(topic, count=3, **kw):
        calls.append(count)
        return [{"title": f"T{len(calls)}", "body_md": "正文内容"}]

    orig = llm._gen_batch
    try:
        llm._gen_batch = fake_gen_batch

        calls.clear()
        out = llm.generate_articles("脱发", 3, provider="ollama",
                                    base_url="http://192.168.123.5:11434/v1", model="qwen2.5:7b")
        check("本地(ollama) 生成 3 篇 → 发起 3 次调用、每次 1 篇",
              calls == [1, 1, 1], f"calls={calls}")
        check("本地逐篇：最终仍产出 3 篇", len(out) == 3, f"out={len(out)}")

        calls.clear()
        out2 = llm.generate_articles("脱发", 3, provider="deepseek",
                                     base_url="https://api.deepseek.com/v1", model="deepseek-chat")
        check("云端(deepseek) 生成 3 篇 → 仅 1 次调用(3篇/次)，原行为不变",
              calls == [3], f"calls={calls} out={len(out2)}")

        calls.clear()
        llm.generate_articles("脱发", 1, provider="ollama",
                              base_url="http://localhost:11434/v1", model="qwen2.5:7b")
        check("本地 count=1 → 仅 1 次调用（无额外开销，定时任务行为不变）",
              calls == [1], f"calls={calls}")

        calls.clear()
        llm.generate_articles("脱发", 2, provider="openai_compat",
                              base_url="https://qwen.dadaobo.com/v1", model="Qwen3.8-27B")
        check("qwen.dadaobo.com(openai_compat) 生成 2 篇 → 2 次单篇调用",
              calls == [1, 1], f"calls={calls}")

        calls.clear()
        llm.generate_articles("脱发", 4, provider="deepseek",
                              base_url="http://192.168.1.9:8000/v1", model="local-model")
        check("兜底：内网 base_url + deepseek provider → 4 次单篇调用",
              calls == [1, 1, 1, 1], f"calls={calls}")
    finally:
        llm._gen_batch = orig

    print("\n" + "=" * 56)
    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"=== 通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
