#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""列出所有标题中含"偏头痛"或"梳子"的文章,做诊断。"""
import os
import json
import ssl
import urllib.request
import urllib.error

APPID = os.getenv("WX_APPID", "")
SECRET = os.getenv("WX_SECRET", "")
BASE = "https://api.weixin.qq.com/cgi-bin"

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


def main():
    # 拿 token
    r = http_get(f"{BASE}/token", {
        "grant_type": "client_credential",
        "appid": APPID,
        "secret": SECRET,
    })
    token = r["access_token"]
    print(f"token: {token}\n")

    # 全部拉出来(no_content=0 才能拿到标题)
    all_items = []
    offset = 0
    while True:
        rr = http_post(f"{BASE}/freepublish/batchget?access_token={token}", {
            "offset": offset,
            "count": 20,
            "no_content": 0,
        })
        items = rr.get("item", [])
        all_items.extend(items)
        total = rr.get("total_count", 0)
        if offset + len(items) >= total or len(items) == 0:
            break
        offset += len(items)

    print(f"总数: {len(all_items)} 条\n")

    # 列出所有标题
    print("=== 所有文章标题 ===")
    for i, it in enumerate(all_items):
        t = it.get("title", "")
        aid = it.get("article_id")
        ut = it.get("update_time")
        deld = it.get("is_deleted")
        print(f"{i+1:3d}. [aid={aid} ut={ut} del={deld}]  {repr(t)}")

    print("\n=== 含'偏头痛'的 ===")
    for it in all_items:
        t = it.get("title", "")
        if "偏头痛" in t:
            print(f"  [aid={it.get('article_id')} ut={it.get('update_time')} del={it.get('is_deleted')}]  {repr(t)}")

    print("\n=== 含'梳子'的 ===")
    for it in all_items:
        t = it.get("title", "")
        if "梳子" in t:
            print(f"  [aid={it.get('article_id')} ut={it.get('update_time')} del={it.get('is_deleted')}]  {repr(t)}")


if __name__ == "__main__":
    main()