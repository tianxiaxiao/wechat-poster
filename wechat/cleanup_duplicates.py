#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""清理重复发布的"我妈偏头痛"文章,只保留最新版本。"""
import os
import json
import time
import urllib.request
import urllib.error
import ssl

APPID = os.getenv("WX_APPID", "")
SECRET = os.getenv("WX_SECRET", "")
TARGET_TITLE = "我妈偏头痛，我最后送了她一把梳子"
BASE = "https://api.weixin.qq.com/cgi-bin"

# 禁用 SSL 证书验证（微信服务器正常,但某些环境证书链不全）
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE


def http_get(url, params=None):
    if params:
        qs = urllib.parse.urlencode(params)
        url = f"{url}?{qs}"
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def http_post(url, payload):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    with urllib.request.urlopen(req, context=CTX, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get_access_token():
    r = http_get(f"{BASE}/token", {
        "grant_type": "client_credential",
        "appid": APPID,
        "secret": SECRET,
    })
    print(f"[access_token] {r}")
    if "access_token" not in r:
        raise RuntimeError(f"获取 access_token 失败: {r}")
    return r["access_token"]


def get_title(it):
    """从 item 中提取标题。freepublish/batchget 的标题在 content.news_item[0].title"""
    c = it.get("content", {})
    if isinstance(c, dict):
        ni = c.get("news_item", [])
        if ni and isinstance(ni, list):
            return ni[0].get("title", "")
    return it.get("title", "")


def get_update_time(it):
    c = it.get("content", {})
    if isinstance(c, dict):
        return c.get("update_time", it.get("update_time", 0))
    return it.get("update_time", 0)


def batchget_all(token):
    """分页拉取所有已发布文章。"""
    all_items = []
    offset = 0
    count = 20
    while True:
        r = http_post(f"{BASE}/freepublish/batchget?access_token={token}", {
            "offset": offset,
            "count": count,
            "no_content": 0,
        })
        print(f"[batchget offset={offset}] total={r.get('total_count')} item_count={r.get('item_count')}")
        items = r.get("item", [])
        all_items.extend(items)
        total = r.get("total_count", 0)
        if offset + len(items) >= total or len(items) == 0:
            break
        offset += len(items)
        time.sleep(0.5)
    return all_items, total


def delete_article(token, article_id):
    r = http_post(f"{BASE}/freepublish/delete?access_token={token}", {
        "article_id": article_id,
    })
    return r


def main():
    token = get_access_token()
    items, total = batchget_all(token)
    print(f"\n=== 共 {total} 条已发布文章 ===\n")

    # 找出所有"我妈偏头痛"标题的文章
    matches = [it for it in items if get_title(it) == TARGET_TITLE]
    print(f'匹配 "{TARGET_TITLE}" 的文章数: {len(matches)}')
    for m in matches:
        print(f"  - article_id={m.get('article_id')}  update_time={m.get('update_time')}  is_deleted={m.get('is_deleted')}")

    if len(matches) <= 1:
        print("\n无需删除（只有 1 条或 0 条）。")
        return

    # 按 update_time 倒序,保留最新
    matches_sorted = sorted(matches, key=lambda x: get_update_time(x), reverse=True)
    keep = matches_sorted[0]
    to_delete = matches_sorted[1:]
    print(f"\n保留（最新）: article_id={keep.get('article_id')}  update_time={keep.get('update_time')}")
    print(f"待删除: {len(to_delete)} 条")

    deleted_ok = []
    deleted_fail = []
    for m in to_delete:
        aid = m.get("article_id")
        ut = get_update_time(m)
        print(f"\n>> 删除 article_id={aid}  update_time={ut}")
        for attempt in range(3):
            r = delete_article(token, aid)
            print(f"   响应: {r}")
            if r.get("errcode") == 0:
                deleted_ok.append((aid, ut, r))
                break
            elif r.get("errcode") == 45009:
                print(f"   命中 45009 (今日 API 配额用尽),退出。")
                return
            elif r.get("errcode") == 40016:
                print(f"   40016 invalid article_id (可能已被手动删除),跳过。")
                deleted_fail.append((aid, ut, r))
                break
            else:
                print(f"   第 {attempt+1} 次重试...")
                time.sleep(1)
        time.sleep(1.1)  # 避免触发 45009

    print("\n=== 删除汇总 ===")
    print(f"成功: {len(deleted_ok)} 条")
    for aid, ut, r in deleted_ok:
        print(f"  ✅ {aid}  update_time={ut}")
    print(f"失败/跳过: {len(deleted_fail)} 条")
    for aid, ut, r in deleted_fail:
        print(f"  ⚠️ {aid}  update_time={ut}  errcode={r.get('errcode')} errmsg={r.get('errmsg')}")

    # 最终核验
    print("\n=== 最终核验 batchget ===")
    time.sleep(1)
    items2, total2 = batchget_all(token)
    matches2 = [it for it in items2 if get_title(it) == TARGET_TITLE]
    print(f'\n"{TARGET_TITLE}" 剩余: {len(matches2)} 条')
    for m in matches2:
        print(f"  - article_id={m.get('article_id')}  update_time={get_update_time(m)}  is_deleted={m.get('is_deleted')}")


if __name__ == "__main__":
    main()