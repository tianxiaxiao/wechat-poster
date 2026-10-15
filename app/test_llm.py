"""
test_llm.py — 单测：调 DeepSeek 生成 3 篇偏头痛软文，输出到 test_articles.json
只调 LLM，不发布微信
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import llm

print("--- DeepSeek API 配置 ---")
print(f"  base_url : {os.getenv('DEEPSEEK_BASE_URL')}")
print(f"  model    : {os.getenv('DEEPSEEK_MODEL')}")
print(f"  api_key  : {os.getenv('DEEPSEEK_API_KEY','')[:6]}...({len(os.getenv('DEEPSEEK_API_KEY',''))} 字符)")
print()

topic = sys.argv[1] if len(sys.argv) > 1 else "偏头痛"
print(f"主题：{topic}")
print("正在调用 DeepSeek...")
articles = llm.generate_articles(topic, count=3, brand="百秘一梳")
print(f"\n--- DeepSeek 返回 {len(articles)} 篇 ---\n")
out = []
for i, a in enumerate(articles):
    print(f"=== 第 {i+1} 篇 ===")
    print(f"标题: {a['title']}")
    print(f"正文 ({len(a['body_md'])} 字符):")
    print(a['body_md'])
    print()
    out.append(a)

out_path = os.path.join(os.path.dirname(__file__), "data", "test_articles.json")
os.makedirs(os.path.dirname(out_path), exist_ok=True)
with open(out_path, "w", encoding="utf-8") as f:
    json.dump({"topic": topic, "articles": out}, f, ensure_ascii=False, indent=2)
print(f"已保存到: {out_path}")
