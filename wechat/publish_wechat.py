# -*- coding: utf-8 -*-
"""
百秘一梳 · 微信公众号(服务号)发布脚本 v2

改动（相对 v1）：
1. 封面图 / 正文配图 改为「从素材库随机抽取」，不再 AI 生成 / 上传本地图片。
2. 增加两种发布方式：
   --publish  发布（draft/add + freepublish/submit）→ 只生成永久链接，不推送、不进主页历史消息
   --mass     群发（media/uploadnews + message/mass/sendall）→ 推送给粉丝 + 进主页历史消息

用法：
  python publish_wechat.py config.json --publish
  python publish_wechat.py config.json --mass

技术要点：
- draft/add 的 thumb_media_id 用「永久素材」media_id（素材库直接可用）。
- media/uploadnews（群发）的 thumb_media_id 必须是「临时素材」media_id，所以需把素材库图片
  下载后经 media/upload 临时上传一次（图片内容仍来自你的素材库，非 AI 生成）。
- 原创声明 / 赞赏：微信官方 API 不开放自动勾选，需在公众号后台手动操作。
"""
import json, os, time, random, argparse
import requests
import markdown

BASE = "https://api.weixin.qq.com"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def post_json(url, token, payload):
    """POST JSON，中文以 UTF-8 原样发送（ensure_ascii=False），防止标题乱码。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    r = requests.post(url, params={"access_token": token}, data=data,
                      headers={"Content-Type": "application/json; charset=utf-8"},
                      timeout=60)
    return r.json()


def get_token(appid, secret):
    log("获取 access_token ...")
    r = requests.get(f"{BASE}/cgi-bin/token",
                     params={"grant_type": "client_credential", "appid": appid, "secret": secret},
                     timeout=30)
    d = r.json()
    if "access_token" not in d:
        raise RuntimeError(f"get_token 失败: {d}")
    log("access_token 获取成功")
    return d["access_token"]


def list_material_images(token):
    """拉取素材库全部图片素材，返回 [{media_id, name, url}]"""
    images = []
    offset, count = 0, 20
    while True:
        d = post_json(f"{BASE}/cgi-bin/material/batchget_material", token,
                      {"type": "image", "offset": offset, "count": count})
        if d.get("errcode", 0) != 0:
            raise RuntimeError(f"batchget_material 失败: {d}")
        items = d.get("item", [])
        if not items:
            break
        for it in items:
            images.append({"media_id": it.get("media_id"),
                           "name": it.get("name"),
                           "url": it.get("url")})
        offset += len(items)
        if offset >= d.get("total_count", 0):
            break
    return images


def pick_random_images(images, n=2):
    """从素材库随机抽 n 张不重复图片。"""
    if not images:
        raise RuntimeError("素材库没有图片，请先在公众号后台「素材库」上传图片")
    if len(images) < n:
        n = len(images)
    return random.sample(images, n)


def upload_temp_image(token, image_bytes):
    """把图片字节临时上传（media/upload），返回临时 media_id（群发封面用）。"""
    up = requests.post(f"{BASE}/cgi-bin/media/upload",
                       params={"access_token": token, "type": "image"},
                       files={"media": ("cover.png", image_bytes, "image/png")},
                       timeout=60)
    d = up.json()
    if "media_id" not in d:
        raise RuntimeError(f"media/upload 失败: {d}")
    return d["media_id"]


def download_image_bytes(url):
    r = requests.get(url, timeout=60)
    if r.status_code != 200:
        raise RuntimeError(f"下载素材库图片失败 HTTP {r.status_code}: {url}")
    return r.content


def md_to_html(md_text, body_image_url):
    if body_image_url:
        md_text = md_text.rstrip() + f"\n\n![pic]({body_image_url})\n"
    return markdown.markdown(md_text, extensions=["tables", "fenced_code", "sane_lists"])


# ---------- 发布模式（仅链接，不推送、不进主页历史消息） ----------
def publish_mode(token, cfg):
    images = list_material_images(token)
    log(f"素材库共 {len(images)} 张图片，将随机选用")
    for art in cfg["articles"]:
        log(f"==== 文章: {art['title']} ====")
        picks = pick_random_images(images, 2)
        cover, body = picks[0], picks[1] if len(picks) > 1 else picks[0]
        log(f"  封面(随机): {cover['name']}")
        log(f"  配图(随机): {body['name']}")
        with open(art["content_md"], "r", encoding="utf-8") as f:
            md_text = f.read()
        meta = {
            "title": art["title"],
            "author": art.get("author", ""),
            "digest": art.get("digest", ""),
            "content": md_to_html(md_text, body["url"]),
            "content_source_url": art.get("content_source_url", ""),
            "thumb_media_id": cover["media_id"],   # draft/add 用永久素材 media_id
            "show_cover_pic": 1,
            "need_open_comment": art.get("need_open_comment", 0),
            "only_fans_can_comment": art.get("only_fans_can_comment", 0),
        }
        d = post_json(f"{BASE}/cgi-bin/draft/add", token, {"articles": [meta]})
        if "media_id" not in d:
            log(f"  draft/add 失败: {d}")
            continue
        r = post_json(f"{BASE}/cgi-bin/freepublish/submit", token, {"media_id": d["media_id"]})
        if r.get("errcode", 0) != 0:
            log(f"  freepublish/submit 失败: {r}")
        else:
            log(f"  发布成功 publish_id={r.get('publish_id')}")
    log("发布完成（注意：发布不推送、不进主页历史消息，仅生成永久链接）")


# ---------- 群发模式（推送 + 进主页历史消息，一次群发最多 8 篇） ----------
def mass_mode(token, cfg):
    images = list_material_images(token)
    log(f"素材库共 {len(images)} 张图片，将随机选用")
    metas = []
    for art in cfg["articles"]:
        log(f"==== 文章: {art['title']} ====")
        picks = pick_random_images(images, 2)
        cover, body = picks[0], picks[1] if len(picks) > 1 else picks[0]
        log(f"  封面(随机): {cover['name']}")
        log(f"  配图(随机): {body['name']}")
        # 群发封面需临时素材：下载素材库图 -> 临时上传
        img_bytes = download_image_bytes(cover["url"])
        temp_thumb = upload_temp_image(token, img_bytes)
        log(f"  封面临时上传成功")
        with open(art["content_md"], "r", encoding="utf-8") as f:
            md_text = f.read()
        metas.append({
            "title": art["title"],
            "author": art.get("author", ""),
            "digest": art.get("digest", ""),
            "content": md_to_html(md_text, body["url"]),
            "content_source_url": art.get("content_source_url", ""),
            "thumb_media_id": temp_thumb,   # uploadnews 用临时 media_id
            "show_cover_pic": 1,
            "need_open_comment": art.get("need_open_comment", 0),
            "only_fans_can_comment": art.get("only_fans_can_comment", 0),
        })
    log("上传图文素材(media/uploadnews) ...")
    d = post_json(f"{BASE}/cgi-bin/media/uploadnews", token, {"articles": metas})
    if "media_id" not in d:
        raise RuntimeError(f"media/uploadnews 失败: {d}")
    media_id = d["media_id"]
    log(f"图文素材上传成功 media_id={media_id}")
    log("提交群发(message/mass/sendall, 全部粉丝) ...")
    payload = {
        "filter": {"is_to_all": True},
        "mpnews": {"media_id": media_id},
        "msgtype": "mpnews",
        "send_ignore_reprint": 0,
    }
    r = post_json(f"{BASE}/cgi-bin/message/mass/sendall", token, payload)
    log(f"群发返回: {r}")
    if r.get("errcode", 0) != 0:
        raise RuntimeError(f"message/mass/sendall 失败: {r}")
    log(f"群发任务已提交 msg_id={r.get('msg_id')} msg_data_id={r.get('msg_data_id')}")
    log("群发完成（已推送给全部粉丝，服务号本月群发额度 -1）")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("config", nargs="?", default="config.json")
    ap.add_argument("--publish", action="store_true", help="发布模式(仅链接)")
    ap.add_argument("--mass", action="store_true", help="群发模式(推送+主页显示)")
    args = ap.parse_args()

    with open(args.config, "r", encoding="utf-8") as f:
        cfg = json.load(f)

    appid = cfg.get("appid") or os.environ.get("WX_APPID")
    secret = cfg.get("secret") or os.environ.get("WX_SECRET")
    if not appid or not secret:
        raise SystemExit("缺少 appid/secret")

    token = get_token(appid, secret)
    if args.mass:
        mass_mode(token, cfg)
    else:
        publish_mode(token, cfg)


if __name__ == "__main__":
    main()
