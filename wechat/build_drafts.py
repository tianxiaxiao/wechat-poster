# -*- coding: utf-8 -*-
"""
建三个草稿（标题/作者/正文/封面全部填好），供浏览器阶段直接点发表。
封面从素材库随机抽取（永久 media_id 用于 draft/add 的 thumb_media_id）。
正文为纯段落 markdown，转成 <p> 段落 HTML。
"""
import json, random, requests, re, sys

BASE = "https://api.weixin.qq.com"
cfg = json.load(open("config.json", encoding="utf-8"))

def get_token():
    r = requests.get(f"{BASE}/cgi-bin/token", params={
        "grant_type": "client_credential",
        "appid": cfg["appid"], "secret": cfg["secret"]}, timeout=30)
    return r.json()["access_token"]

def post_json(url, token, payload):
    r = requests.post(url, params={"access_token": token},
                      data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                      headers={"Content-Type": "application/json"}, timeout=30)
    r.encoding = "utf-8"
    return r.json()

def md_to_html(md_text):
    """纯段落 markdown -> <p> 段落 HTML"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", md_text) if p.strip()]
    return "".join(f"<p>{p}</p>" for p in paras)

tok = get_token()

# 1) 素材库图片（含永久 media_id）
d = post_json(f"{BASE}/cgi-bin/material/batchget_material", tok,
              {"type": "image", "offset": 0, "count": 20})
imgs = d.get("item", [])
print("素材库图片数:", len(imgs))
random.shuffle(imgs)

draft_ids = []
for i, art in enumerate(cfg["articles"]):
    cover = imgs[i % len(imgs)]
    print(f"\n=== 第{i+1}篇：{art['title']} ===")
    print(f"  封面: {cover.get('name')}")

    md_text = open(art["content_md"], encoding="utf-8").read()
    html = md_to_html(md_text)

    payload = {
        "articles": [{
            "title": art["title"],
            "author": art["author"],
            "digest": art["digest"],
            "content": html,
            "content_source_url": art.get("content_source_url", ""),
            "thumb_media_id": cover["media_id"],
            "need_open_comment": art.get("need_open_comment", 0),
            "only_fans_can_comment": art.get("only_fans_can_comment", 0),
        }]
    }
    r = post_json(f"{BASE}/cgi-bin/draft/add", tok, payload)
    if "media_id" in r:
        print(f"  草稿建好 media_id = {r['media_id']}")
        draft_ids.append({"title": art["title"], "media_id": r["media_id"]})
    else:
        print(f"  建草稿失败: {r}")

json.dump(draft_ids, open("draft_ids.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\n草稿 media_id 已保存到 draft_ids.json")
