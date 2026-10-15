"""
历史文章相似度 / 去重过滤（纯 Python，无第三方依赖，中文友好）
================================================
用途：调 LLM 生成软文前，避免与历史已发布文章"标题雷同 / 内容撞车"。

两层用法（见 app._gen_with_dedup）：
  1) 提示词层：load_history_titles() 取最近 N 条历史标题注入 LLM prompt，明确禁止雷同；
  2) 后过滤层：check_article() 对生成结果逐篇 vs 历史做相似度检测，超阈值自动重写一次。

相似度算法：字符 bigram（相邻两字一组）的 Jaccard 系数。
中文标题 16-26 字，bigram 对"句式/用词重复"非常敏感；
完全不同的标题一般在 0.2 以下，改写雷同的通常 >0.5。
"""
import re
from typing import List, Dict, Optional

# ---- 可调阈值 ----
TITLE_SIM_THRESHOLD = 0.50   # 标题 bigram 相似度超过此值 → 判定与历史标题雷同
BODY_SIM_THRESHOLD = 0.60    # 正文开头采样段相似度超过此值 → 判定内容撞车
BODY_SAMPLE_CHARS = 400      # 正文取开头 N 个字符参与比对（结尾钩子句式相同属正常）
HISTORY_LIMIT = 200          # 最多加载最近 N 篇历史文章参与比对

# 去掉空白/标点后比对，避免标点差异影响相似度
_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    return _PUNCT.sub("", (text or "").lower())


def _bigrams(text: str) -> set:
    t = normalize(text)
    if len(t) < 2:
        return {t} if t else set()
    return {t[i:i + 2] for i in range(len(t) - 1)}


def similarity(a: str, b: str) -> float:
    """两段文本的 bigram Jaccard 相似度 [0,1]"""
    ba, bb = _bigrams(a), _bigrams(b)
    if not ba or not bb:
        return 0.0
    return len(ba & bb) / len(ba | bb)


def load_history(limit: int = HISTORY_LIMIT) -> List[Dict]:
    """从 Article 表加载最近 limit 篇历史文章（含所有状态：generated/published/...）。
    返回 [{"title":..., "body":...}]，按创建时间倒序（越新越靠前）。"""
    try:
        from db import Article, SessionLocal
    except Exception:
        return []
    s = SessionLocal()
    try:
        rows = (s.query(Article.title, Article.body_md)
                .order_by(Article.created_at.desc(), Article.id.desc())
                .limit(limit).all())
        out = []
        for title, body in rows:
            if title or body:
                out.append({"title": title or "", "body": body or ""})
        return out
    except Exception:
        return []
    finally:
        s.close()


def load_history_titles(limit: int = 30) -> List[str]:
    """取最近 limit 条历史标题（注入 LLM 提示词用，控制 token 量）"""
    return [h["title"] for h in load_history(limit=limit) if h["title"]]


def check_article(title: str, body: str, history: List[Dict],
                  title_th: float = TITLE_SIM_THRESHOLD,
                  body_th: float = BODY_SIM_THRESHOLD) -> Dict:
    """单篇生成结果 vs 历史文章 相似度检测。
    返回 {"ok": bool, "title_sim": float, "matched_title": str,
          "body_sim": float, "matched_body_title": str}"""
    body_sample = (body or "")[:BODY_SAMPLE_CHARS]
    max_ts, match_t, max_bs, match_b = 0.0, "", 0.0, ""
    for h in history:
        ts = similarity(title or "", h.get("title", ""))
        if ts > max_ts:
            max_ts, match_t = ts, h.get("title", "")
        bs = similarity(body_sample, (h.get("body", "") or "")[:BODY_SAMPLE_CHARS])
        if bs > max_bs:
            max_bs, match_b = bs, h.get("title", "")
    ok = (max_ts < title_th) and (max_bs < body_th)
    return {"ok": ok, "title_sim": max_ts, "matched_title": match_t,
            "body_sim": max_bs, "matched_body_title": match_b}


def dedup_report(items: List[Dict], history: List[Dict]) -> List[Dict]:
    """批量检测：给每篇文章附加 sim 检测结果字段（不修改原列表，返回带 _sim 的副本）"""
    out = []
    for it in items:
        r = dict(it)
        r["_sim"] = check_article(it.get("title", ""), it.get("body_md", ""), history)
        out.append(r)
    return out
