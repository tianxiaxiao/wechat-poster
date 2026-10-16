"""
wechat_api.py — 微信公众平台官方 API 客户端（免扫码全自动发布通道）
核心能力：
  1. get_access_token()            获取/缓存 access_token
  2. list_cover_media()            从永久图片素材库随机取一张封面 thumb_media_id
  3. add_draft(title, html, ...)   存为草稿 → 返回 draft media_id
  4. publish_draft(media_id)       调用 freepublish/submit 把草稿发布到公众号"发表记录"
                                   不主动推送给粉丝、不占每月群发额度、无需扫码
全程用 Python 标准库 urllib，不依赖第三方包。
"""
import json
import os
import random
import re
import time
import urllib.request
import urllib.parse
import logging
from pathlib import Path

log = logging.getLogger("poster.wxapi")

# 当前账号凭证：**只从环境变量读，不内置任何默认值**（开源版不携带任何真实凭证）。
# 可用 set_account() 在运行时切换，实现多公众号。
_ACCOUNT = {
    "appid": os.getenv("WX_APPID", ""),
    "secret": os.getenv("WX_SECRET", ""),
}
_token_cache: dict = {}   # appid -> {"token":..., "expire":...}


def set_account(appid: str, secret: str):
    """切换到指定公众号账号（appid + secret）。多公众号发布前调用。"""
    if not appid or not secret:
        raise ValueError("set_account 需要 appid 和 secret")
    _ACCOUNT["appid"] = appid
    _ACCOUNT["secret"] = secret


def get_account() -> tuple:
    return _ACCOUNT["appid"], _ACCOUNT["secret"]


# 强制直连 opener：忽略环境/系统代理（HTTP_PROXY 等）。
# 背景：代理出口 IP 与本机宽带 IP 不同且不稳定，会导致微信 40164（IP 不在白名单）；
# 直连出口 = 本机宽带真实 IP，白名单按宽带 IP（或其网段）配置即可稳定生效。
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _http_json(url: str, body=None, method="POST") -> dict:
    """POST/GET JSON 并解析（强制直连，不走代理）"""
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method=method)
    else:
        req = urllib.request.Request(url, method=method)
    with _OPENER_DIRECT.open(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def parse_probe_error(d: dict) -> dict:
    """把微信的错误响应解析成「可读结论」——纯函数，便于离线单测。
    最有用的是 40164（IP 不在白名单）：微信的 errmsg 里**带着它看到的那个 IP**，
    直接告诉用户该往白名单里加什么。"""
    errcode = int(d.get("errcode") or 0)
    errmsg = str(d.get("errmsg") or "")
    ip = ""
    m = re.search(r"invalid ip\s*([0-9.]+)", errmsg, re.I) or re.search(r"([0-9]{1,3}(?:\.[0-9]{1,3}){3})", errmsg)
    if m:
        ip = m.group(1)
    if errcode == 40164:
        hint = (f"出口 IP {ip or '（见下方原文）'} 不在公众号 IP 白名单 —— "
                f"请到微信开发者平台「公众号/服务号 → 基础信息 → API IP白名单」把它加上"
                f"（支持网段，如 106.85.0.0/16，加网段就不用频繁改）")
    elif errcode in (40013, 40125, 41004):
        hint = "appid 或 AppSecret 不正确（请核对「微信开发者平台-公众号/服务号-基础信息」里的值）"
    elif errcode == 40125:
        hint = "AppSecret 不正确"
    elif errcode:
        hint = f"微信接口报错 {errcode}：{errmsg[:120]}"
    else:
        hint = "未能取得 access_token（返回体异常）"
    return {"ok": False, "errcode": errcode, "err": errmsg[:300], "ip": ip, "hint": hint}


def probe_credentials(appid: str, secret: str, timeout: int = 12) -> dict:
    """**无副作用**地验证「appid/secret 是否正确 + 本机出口 IP 是否已在白名单」。

    为什么单独写一个：取 access_token 本来就是验证白名单最轻的手段（不发任何文章），
    但 get_access_token() 用的是模块级全局 _ACCOUNT + token 缓存，
    在定时批次发布过程中被并发调用会**把账号切错**。所以这里直接带参请求，完全不碰全局状态。

    返回 {"ok": bool, "errcode": int, "err": str, "ip": str, "hint": str}
    """
    appid = (appid or "").strip()
    secret = (secret or "").strip()
    if not appid or not secret:
        return {"ok": False, "errcode": 0, "err": "未提供 appid / AppSecret",
                "ip": "", "hint": "该公众号没有可用的 appid/AppSecret"}
    try:
        d = _http_json("https://api.weixin.qq.com/cgi-bin/stable_token",
                       {"grant_type": "client_credential", "appid": appid, "secret": secret,
                        "force_refresh": False})
    except Exception as e:  # noqa: BLE001 —— 网络不通时也算"验证失败"，但要说清是网络问题
        return {"ok": False, "errcode": 0, "err": str(e)[:200], "ip": "",
                "hint": "请求微信接口失败（网络/DNS/超时）：请检查 NAS 能否访问 api.weixin.qq.com"}
    if d.get("access_token"):
        return {"ok": True, "errcode": 0, "err": "", "ip": "", "hint": "凭证有效，且出口 IP 已在白名单"}
    return parse_probe_error(d)


def get_access_token(force: bool = False) -> str:
    """获取 access_token。

    优先用微信推荐的 getStableAccessToken（/cgi-bin/stable_token）：
    该接口服务端会尽量复用稳定 token，避免"多次刷新互相作废旧 token"导致的
    40001 invalid credential / access_token not latest。
    """
    appid, secret = _ACCOUNT["appid"], _ACCOUNT["secret"]
    if not appid or not secret:
        raise RuntimeError(
            "未配置公众号凭证：请设置环境变量 WX_APPID / WX_SECRET（或在网页「📱 公众号」里添加账号）。"
            "详见 .env.example")
    c = _token_cache.get(appid)
    if not force and c and c["token"] and time.time() < c["expire"]:
        return c["token"]
    body = {
        "grant_type": "client_credential",
        "appid": appid,
        "secret": secret,
        "force_refresh": force,
    }
    d = _http_json("https://api.weixin.qq.com/cgi-bin/stable_token", body)
    if "access_token" not in d:
        # stable_token 偶发不可用(如未开通)时退回普通 /cgi-bin/token
        url = f"https://api.weixin.qq.com/cgi-bin/token?grant_type=client_credential&appid={appid}&secret={secret}"
        d = _http_json(url, method="GET")
    if "access_token" not in d:
        raise RuntimeError(f"获取 access_token 失败: {d}")
    _token_cache[appid] = {"token": d["access_token"], "expire": time.time() + d.get("expires_in", 7200) - 300}
    return _token_cache[appid]["token"]


def _api(path: str, body: dict, _retry: bool = True, _attempt: int = 0) -> dict:
    """调微信 API；内置两类自动重试：
    - 40001/40014（token 失效/过期）：强制刷新 token 后重试一次
    - 53501（请勿频繁请求/频控）：等待后指数退避重试，最多约 3 分钟
    """
    tok = get_access_token()
    url = f"https://api.weixin.qq.com/cgi-bin/{path}?access_token={tok}"
    d = _http_json(url, body)
    if not _retry:
        return d
    code = d.get("errcode")
    if code in (40001, 40014) and _attempt < 2:
        log.warning("%s 返回 %s，强制刷新 token 后重试…", path, code)
        get_access_token(force=True)
        return _api(path, body, _retry=True, _attempt=_attempt + 1)
    if code == 53501 and _attempt < 6:
        wait = 20 * (2 ** _attempt)  # 20s, 40s, 80s, 160s, 320s… 到第 6 次封顶
        if wait > 90:
            wait = 90
        log.warning("%s 返回 53501(频繁请求)，%.0fs 后自动重试(%d/6)…", path, wait, _attempt + 1)
        time.sleep(wait)
        return _api(path, body, _retry=True, _attempt=_attempt + 1)
    return d


def ensure_ok(d: dict, ctx: str = ""):
    if d.get("errcode", 0) != 0:
        raise RuntimeError(f"{ctx} 失败: errcode={d.get('errcode')} errmsg={d.get('errmsg')} ({ctx})")
    return d


def list_cover_media() -> list:
    """列出永久图片素材（可作封面）。返回 [{media_id, name, url}]"""
    d = _api("material/batchget_material", {"type": "image", "offset": 0, "count": 20})
    if "item" not in d:
        return []
    return [{"media_id": it.get("media_id"), "name": it.get("name"), "url": it.get("url")}
            for it in d.get("item", []) if it.get("media_id")]


def pick_random_cover_media() -> str:
    """随机取一张封面 thumb_media_id；无素材时返回空串"""
    covers = list_cover_media()
    if not covers:
        log.warning("素材库无图片素材，封面 media_id 为空")
        return ""
    return random.choice(covers)["media_id"]


def add_draft(title: str, content_html: str, author: str = "百秘一梳",
              digest: str = "", thumb_media_id: str = "", need_open_comment: int = 0,
              only_fans_can_comment: int = 0) -> str:
    """
    存为草稿。返回 draft 的 media_id。
    官方 draft/add 需要: articles:[{title, author, digest, content, thumb_media_id, ...}]
    """
    if not title or not content_html:
        raise ValueError("title/content_html 不能为空")
    article = {
        "title": title,
        "author": author,
        "digest": digest[:120],
        "content": content_html,
        "content_source_url": "",
        "need_open_comment": need_open_comment,
        "only_fans_can_comment": only_fans_can_comment,
    }
    if thumb_media_id:
        article["thumb_media_id"] = thumb_media_id
        article["show_cover_pic"] = 1
    else:
        article["thumb_media_id"] = ""
        article["show_cover_pic"] = 0
    d = _api("draft/add", {"articles": [article]})
    ensure_ok(d, "draft/add")
    media_id = d.get("media_id", "")
    if not media_id:
        raise RuntimeError("draft/add 未返回 media_id")
    return media_id


def publish_draft(media_id: str, fast_fail: bool = False) -> dict:
    """
    把草稿发布到公众号"发表记录"(freepublish)。
    不主动推送给粉丝、不占每月群发额度、无需扫码。
    fast_fail=True：遇 53501 频控不长时间退避，立即返回失败（供每日分批任务使用——
    该场景失败篇会保留草稿，30 分钟后自动续批重试，不需要篇内长时间等待）。
    返回 {"publish_id": ...}
    """
    d = _api("freepublish/submit", {"media_id": media_id}, _retry=not fast_fail)
    ensure_ok(d, "freepublish/submit")
    return {"publish_id": d.get("publish_id"), "msg_data_id": d.get("msg_data_id")}


def fetch_today_published(limit: int = 40) -> list:
    """取"今天（北京时间）已发布"的文章列表，用于**多设备同时使用同一公众号时的防重复发布**。

    判断依据：freepublish/batchget 返回的 item.update_time（发表时会刷新为该时刻），
    与北京时间当天 00:00 比较。跨设备场景下本机数据库看不到另一台机的发布记录，
    但微信端记录是同一份——据此可发现"今天已经发过 N 篇"。

    返回 [{"title": str, "ts": int}]（较新的在前）；接口异常时抛出，由调用方兜底。
    """
    now_ts = int(time.time())
    bj = time.gmtime(now_ts + 8 * 3600)                      # 北京时间
    day_start_ts = now_ts - (bj.tm_hour * 3600 + bj.tm_min * 60 + bj.tm_sec)   # 今晨 00:00（北京）的 UTC 时间戳
    out, offset = [], 0
    while len(out) < limit and offset < 60:
        page = min(20, limit - len(out))
        d = _api("freepublish/batchget", {"offset": offset, "count": page, "no_content": 1})
        items = d.get("item") or []
        if not items:
            break
        for it in items:
            ts = int(it.get("update_time") or 0)
            if ts and ts >= day_start_ts:
                try:
                    t = ((it.get("content") or {}).get("news_item") or [{}])[0].get("title") or ""
                except Exception:
                    t = ""
                out.append({"title": t, "ts": ts})
        if len(items) < page:
            break
        offset += page
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out


def fetch_recent_urls(limit: int = 40) -> dict:
    """批量获取"最近已发布文章"的永久链接映射（分页，单页上限 20 篇）。

    ⚠️ 为什么不用 fetch_publish_url(逐篇 freepublish/get)：
       该接口日配额很小，一篇一篇轮询（每篇 4 次）在批量发布时极易触发
       45009「reach max api daily quota limit」，导致大部分文章抓不到链接。
       而 freepublish/batchget 一次调用最多返回 20 篇，且 news_item.url 就是永久链接。

    匹配方式：链接里的 mid 参数 == freepublish/submit 返回的 publish_id（精确）；
             标题作为兜底。
    ⚠️ 实测 mid 与 publish_id 并不相等，by_mid 无法用来匹配 Article.publish_id；
       按 publish_id 取链接请直接用 fetch_publish_url(publish_id)（freepublish/get）。
    返回 {"by_mid": {mid: url}, "by_title": {title: url}}
    """
    by_mid, by_title = {}, {}
    got = 0
    offset = 0
    while got < limit and offset < 60:          # 最多翻 3 页，防异常失控
        page = min(20, limit - got)
        try:
            d = _api("freepublish/batchget",
                     {"offset": offset, "count": page, "no_content": 0})
        except Exception as e:  # noqa: BLE001
            log.warning("批量取文章链接失败(网络)：%s", str(e)[:100])
            break
        items = d.get("item") or []
        if not items:
            break
        for it in items:
            try:
                n = (it.get("content") or {}).get("news_item", [])[0]
            except Exception:
                continue
            u = str(n.get("url") or "").strip()
            if not u:
                continue
            u = u.replace("http://", "https://", 1)
            if u.endswith("#rd"):
                u = u[:-3]
            m = re.search(r"[?&]mid=(\d+)", u)
            if m:
                by_mid[m.group(1)] = u
            t = str(n.get("title") or "").strip()
            if t:
                by_title[t] = u
            got += 1
        if len(items) < page:
            break
        offset += page
    return {"by_mid": by_mid, "by_title": by_title}


def fetch_publish_url(publish_id) -> str:
    """发布成功后获取单篇文章的永久链接（发布是异步的，必要时轮询几次）。
    返回 https 链接；拿不到返回空串（不影响发布流程）。

    ⚠️ 该接口（freepublish/get）日配额很小，批量场景请优先用 fetch_recent_urls()
       一次批量取回，避免逐篇调用触发 45009 配额耗尽。"""
    try:
        pid = int(publish_id)
    except Exception:
        return ""
    for _ in range(2):
        try:
            d = _api("freepublish/get", {"publish_id": pid})
        except Exception as e:
            log.warning("取文章链接失败(网络)：%s", str(e)[:80])
            time.sleep(2)
            continue
        st = d.get("publish_status")
        if st == 0:  # 0 = 发布成功
            items = ((d.get("article_detail") or {}).get("item") or [])
            if items:
                url = (items[0].get("article_url") or "").strip()
                return url.replace("http://", "https://", 1) if url else ""
            return ""
        if st == 1:  # 1 = 发布中，稍后再查
            time.sleep(3)
            continue
        break  # 其他状态（审核/失败）没有链接
    return ""


def publish_get(publish_id: int) -> dict:
    d = _api("freepublish/get", {"publish_id": publish_id})
    return d


def find_draft_media_id_by_title(title: str, max_pages: int = 3, retries: int = 30, retry_delay: float = 6.0,
                                 fallback_latest: bool = True) -> str:
    """从草稿箱按标题找最近一条草稿的 media_id。
    publisher.js 保存草稿后调用；微信草稿索引有延迟，会带延时重试。
    fallback_latest=True：找不到精确标题时，若草稿箱非空则返回最新一篇的 media_id
    （playwright 刚保存的草稿必然是草稿箱最新一篇）。"""
    for attempt in range(retries):
        all_titles = []
        try:
            for offset in range(0, max_pages * 20, 20):
                d = _api("draft/batchget", {"offset": offset, "count": 20, "no_content": 1})
                for it in d.get("item", []):
                    try:
                        n = it["content"]["news_item"][0]
                        t = (n.get("title") or "").strip()
                        all_titles.append(t)
                        if t == title.strip():
                            return it.get("media_id", "")
                    except Exception:
                        continue
                if len(d.get("item", [])) < 20:
                    break
        except Exception as e:
            log.warning("find_draft 查询异常 %s", e)
        if attempt < retries - 1:
            log.info("未精确匹配标题（attempt %d/%d），%.0fs 后重试。草稿箱: %s", attempt + 1, retries, retry_delay, all_titles[-3:])
            time.sleep(retry_delay)
        elif fallback_latest and all_titles:
            # 末尾兜底：取最新一篇（刚保存的草稿通常最新）
            log.info("标题 %r 未匹配，兜底返回草稿箱最新一篇（标题 %r）", title, all_titles[0])
            return _latest_draft_media_id()
    return ""


def _latest_draft_media_id() -> str:
    """返回草稿箱最新一篇的 media_id"""
    d = _api("draft/batchget", {"offset": 0, "count": 1, "no_content": 1})
    items = d.get("item", [])
    return items[0].get("media_id", "") if items else ""


_STYLE_CSS = {
    "primary": "#2f9e5f",      # 主色（品牌绿）
    "primary_bg": "#e9f7ef",
    "accent": "#d9822b",       # 强调橙（痛点/提醒）
    "accent_bg": "#fdf0e0",
    "tip_bg": "#eef6ff",       # 知识点/方法蓝
    "tip_border": "#5b9bd5",
    "text": "#3a3a3a",
    "light": "#8a8a8a",
    "divider": "#e4e7ec",
}


def md_to_html(md: str, accent: str = None) -> str:
    """富排版 markdown → 微信公众号 HTML（带颜色风格）。
    支持：空行分段 / **加粗**(主色) / >引用(左侧彩条重点框) /
          ##~#### 小标题(色块) / - 列表 / --- 分隔线 / 【】引导小标(着色短行)。
    排版颜色用内联 style，公众号富文本基本可保留。"""
    if not md:
        return ""
    import re
    C = dict(_STYLE_CSS)
    if accent:
        C["primary"] = accent
    lines = md.replace("\r\n", "\n").split("\n")
    out = []
    para = []
    in_list = False

    def _inline(t):
        t = re.sub(r"\*\*(.+?)\*\*", lambda m: f'<strong style="color:{C["primary"]}">{m.group(1)}</strong>', t)
        t = re.sub(r"\*(.+?)\*", r"<em>\1</em>", t)
        return t

    def flush_para():
        nonlocal para
        if para:
            txt = "".join(para).strip()
            if txt:
                out.append(f'<p style="font-size:15px;line-height:1.9;color:{C["text"]};margin:0 0 18px 0;">{_inline(txt)}</p>')
            para = []

    def flush_list():
        nonlocal in_list
        if in_list:
            out.append("</ul>")
            in_list = False

    for raw in lines:
        line = raw.rstrip()
        s = line.strip()
        if not s:
            flush_para(); flush_list(); continue
        # 分隔线
        if re.fullmatch(r"[-*_]{3,}", s):
            flush_para(); flush_list()
            # 分隔线：不用 <hr>（微信服务端会把它规范成自闭合 <hr ... />，
            # Windows 客户端渲染时会把 "/" 当正文显示）→ 用 <section> + 上边框画线
            out.append(f'<section style="border-top:1px dashed {C["divider"]};'
                       f'margin:22px 0;height:0;font-size:0;line-height:0;"></section>')
            continue
        # 小标题（#…）
        hm = re.match(r"^(#{1,4})\s+(.+)$", s)
        if hm:
            flush_para(); flush_list()
            t = _inline(hm.group(2).strip())
            out.append(f'<h3 style="font-size:17px;font-weight:700;color:{C["primary"]};border-left:4px solid {C["primary"]};background:{C["primary_bg"]};padding:8px 12px;margin:22px 0 14px 0;border-radius:0 6px 6px 0;">{t}</h3>')
            continue
        # 引用（重点框）
        if s.startswith(">"):
            flush_para(); flush_list()
            t = _inline(s[1:].strip())
            out.append(f'<blockquote style="margin:16px 0;padding:12px 14px;border-left:4px solid {C["accent"]};background:{C["accent_bg"]};color:{C["text"]};border-radius:0 6px 6px 0;font-size:15px;line-height:1.8;">{t}</blockquote>')
            continue
        # 列表
        if re.match(r"^[-*+]\s+", s) or re.match(r"^\d+[.、]\s+", s):
            flush_para()
            if not in_list:
                out.append(f'<ul style="margin:8px 0 18px 0;padding-left:18px;color:{C["text"]};font-size:15px;line-height:2;">')
                in_list = True
            out.append(f'<li>{_inline(re.sub(r"^[-*+]\s+|^\d+[.、]\s+", "", s))}</li>')
            continue
        # 【】引导的短行（提示性小标，居中/点缀）
        if s.startswith("【") and s.endswith("】") and len(s) <= 30:
            flush_para(); flush_list()
            t = _inline(s[1:-1].strip())
            out.append(f'<div style="text-align:center;color:{C["tip_border"]};font-size:14px;font-weight:600;letter-spacing:2px;margin:18px 0;">— {t} —</div>')
            continue
        if in_list: flush_list()
        para.append(line)
    flush_para(); flush_list()
    return "\n".join(out)
