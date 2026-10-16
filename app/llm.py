"""
LLM 生成器 — 支持多种 OpenAI 兼容 provider
- DeepSeek (默认)
- Ollama (本地 qwen/llama 等，默认 http://localhost:11434/v1)
- 任意 OpenAI 兼容端点（vLLM / llama.cpp server / LM Studio / OneAPI 等）

调用方式：
    llm.generate_articles(topic, count, brand=..., extra_hint=..., provider="ollama", model="qwen2.5:7b")
"""
import os
import re
import json
import logging
import urllib.request
from typing import List, Dict, Optional
from urllib.parse import urlparse
from openai import OpenAI

log = logging.getLogger("poster.llm")

# 直连 opener（显式清空代理）：本机 / 内网地址必须直连 ——
# 系统代理通常访问不到 127.0.0.1 / 192.168.x，会把请求变成 502 之类的无用错误。
_OPENER_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _urlopen_direct(req, timeout: int = 30):
    """直连请求（OpenerDirector 本身不可 call，需走 .open()）。"""
    return _OPENER_DIRECT.open(req, timeout=timeout)

# ---- 通用参数 ----
# 单次 LLM 调用最长等待（秒）：超时即判失败，避免端点卡死时整批无意义干转。
# 本地大模型（如 35B Qwen3）一次生成多篇较慢，默认给 180s；可用 LLM_TIMEOUT_SEC 覆盖。
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT_SEC", "180"))
# 浏览器 UA + Origin：部分网关/Cloudflare 会拦截 curl/SDK 默认 UA（HTTP 403 error 1010），
# 统一带浏览器头，OpenAI 兼容端点才通。
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ---- provider 预设 ----
PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek (云端)",
        "default_base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "needs_api_key": True,
        "default_api_key_env": "DEEPSEEK_API_KEY",
    },
    "ollama": {
        "label": "Ollama (本地)",
        "default_base_url": "http://localhost:11434/v1",
        "default_model": "qwen2.5:7b",
        "needs_api_key": False,  # Ollama 本地一般不校验 key
        "default_api_key_env": "OLLAMA_API_KEY",
    },
    "openai_compat": {
        "label": "OpenAI 兼容 (通用)",
        # 通用 OpenAI 兼容网关地址；建议用环境变量 OPENAI_COMPAT_BASE_URL 覆盖
        "default_base_url": os.getenv("OPENAI_COMPAT_BASE_URL", ""),
        "default_model": "Qwen3.8-27B",
        "needs_api_key": True,
        "default_api_key_env": "OPENAI_COMPAT_API_KEY",
    },
    "agnes": {
        "label": "Agnes AI (云端)",
        "default_base_url": "https://api.agnes-ai.cn/v1",
        "default_model": "agnes-3.0-flash",
        "needs_api_key": True,
        "default_api_key_env": "AGNES_API_KEY",
    },
}


def _make_client(provider: str, api_key: str = "", base_url: str = "",
                 allow_env_key: bool = True) -> OpenAI:
    """构造 OpenAI 客户端；缺省值取自环境变量 + provider 默认值。
    ⚠️ Key 优先级：**显式传入的 api_key 优先于环境变量**（2026-10-16 修正）。
    原先是 env 优先，导致「用户自己填了 Key 也用不上、永远被服务端 Key 顶掉」——
    多租户下必须各人用各人的 Key；是否允许回落到服务端 Key 由 app 层决定后显式传入。
    allow_env_key=False → 完全不读 .env 的 Key（未获授权的用户，防止偷用管理员的 Key）。"""
    cfg = PROVIDERS.get(provider, PROVIDERS["deepseek"])
    env_base = os.getenv(cfg["default_base_url"].upper().replace("DEFAULT_BASE_URL", "BASE_URL"), "")
    env_key = os.getenv(cfg["default_api_key_env"], "").strip() if allow_env_key else ""
    if provider == "deepseek":
        final_base_url = cfg["default_base_url"]  # 固定官方地址
        final_key = api_key or env_key
    elif provider == "openai_compat":
        final_base_url = (base_url or env_base or cfg["default_base_url"]).strip()
        final_key = api_key or env_key
    else:
        final_base_url = (base_url or env_base or cfg["default_base_url"]).strip()
        final_key = api_key
        if provider == "ollama" and not base_url:
            final_base_url = cfg["default_base_url"]
        # Ollama 本地一般不校验 key，传 "ollama" 占位即可
        if not final_key:
            final_key = env_key or ("ollama" if provider == "ollama" else "")
    # 本机/内网地址：让 SDK 直连（trust_env=False 即忽略 HTTP_PROXY），
    # 否则局域网自建模型会被系统代理搅乱（返回 502 等）。公网地址保持原行为。
    _kw = {}
    if is_intranet_url(final_base_url):
        try:
            import httpx
            _kw["http_client"] = httpx.Client(trust_env=False, timeout=LLM_TIMEOUT)
        except Exception:  # noqa: BLE001 —— httpx 不可用则退回默认（不影响公网场景）
            pass
    return OpenAI(api_key=final_key or "EMPTY", base_url=final_base_url,
                  default_headers={"User-Agent": BROWSER_UA}, **_kw)


def _urllib_chat(base_url: str, api_key: str, model: str, messages: list,
                 temperature: float = 0.85, max_tokens: int = 8000,
                 extra_body: dict = None, timeout: int = LLM_TIMEOUT) -> str:
    """基于 urllib 的 OpenAI 兼容 chat/completions 调用。
    用于自建 OpenAI 兼容网关 / 本地 Ollama 等：
    - 统一带浏览器 UA + Origin（部分网关/Cloudflare 会拦截 curl/SDK 默认 UA，报 403 error 1010）；
    - 默认超时 LLM_TIMEOUT，端点卡死时快速失败而非无限等待；
    - 本机/内网地址强制直连（绕过系统代理）。"""
    o = urlparse(base_url)
    origin = f"{o.scheme}://{o.netloc}" if o.netloc else ""
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if extra_body:
        payload.update(extra_body)
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": BROWSER_UA,   # 关键：绕过网关/Cloudflare 的 UA 拦截（403 error 1010）
        "Accept": "*/*",
        "Origin": origin,
        "Referer": origin + "/",
    })
    # 本机/内网地址直连（绕过系统代理），公网地址保持默认行为
    _open = _urlopen_direct if is_intranet_url(url) else urllib.request.urlopen
    with _open(req, timeout=timeout) as resp:
        d = json.loads(resp.read().decode("utf-8"))
    try:
        return (d["choices"][0]["message"]["content"] or "").strip()
    except Exception as e:
        raise RuntimeError(f"LLM 返回异常结构: {json.dumps(d, ensure_ascii=False)[:300]}")


def list_providers() -> List[Dict]:
    """给前端返回可选 provider 列表"""
    return [{"id": k, "label": v["label"], "default_base_url": v["default_base_url"],
             "default_model": v["default_model"], "needs_api_key": v["needs_api_key"]}
            for k, v in PROVIDERS.items()]


def describe_config(provider: str, api_key: str = "", base_url: str = "", model: str = "",
                    key_origin: str = "", allow_env_key: bool = True) -> Dict:
    """返回「实际会生效」的连接配置（与真正生成时的优先级**完全一致**），
    供前端「测试连通」回显 —— 避免用户填了 Key/地址却不知真正生效的是哪一个。
    只回显 key 的来源，绝不回显 key 本身。
    优先级（与 generate_articles / ping / _make_client 对齐，2026-10-16 起「显式传入优先」）：
      · deepseek      → base 固定官方；key: 传入 > env
      · openai_compat → base: 传入 > env > 预设；key: 传入 > env
      · ollama 等     → base: 传入 > env > 预设；key: 传入 > env > 占位 ollama
    key_origin：由 app 层按多租户授权策略判定后传入（如「你自己的」「管理员共享」），
    给了就以它为准 —— 因为"能不能用 env 里那把 Key"是 app 层的策略问题，llm 层不该猜。"""
    cfg = PROVIDERS.get(provider, PROVIDERS["deepseek"])
    env_base = os.getenv(cfg["default_base_url"].upper().replace("DEFAULT_BASE_URL", "BASE_URL"), "")
    env_key = os.getenv(cfg["default_api_key_env"], "").strip() if allow_env_key else ""
    if provider == "deepseek":
        final_base = cfg["default_base_url"]              # 官方地址固定
        final_key = api_key or env_key
        key_src = "你填写的" if api_key else ("服务端环境变量" if env_key else "未设置")
    elif provider == "openai_compat":
        final_base = (base_url or env_base or cfg["default_base_url"]).strip()
        final_key = api_key or env_key
        key_src = "你填写的" if api_key else ("服务端环境变量" if env_key else "未设置")
    else:  # ollama 等
        final_base = (base_url or env_base or cfg["default_base_url"]).strip()
        final_key = api_key
        if provider == "ollama" and not base_url:
            final_base = cfg["default_base_url"]
        if not final_key:
            final_key = env_key or ("ollama" if provider == "ollama" else "")
        key_src = "你填写的" if api_key else ("服务端环境变量" if env_key else ("占位(ollama)" if provider == "ollama" else "未设置"))
    env_model_var = cfg.get("default_model_env") or (provider.upper() + "_MODEL")
    final_model = (model or os.getenv(env_model_var, "") or cfg["default_model"]).strip()
    return {
        "provider": provider,
        "base_url": final_base,
        "model": final_model,
        "has_key": bool(final_key),
        "key_source": key_origin or key_src,
        "needs_api_key": bool(cfg.get("needs_api_key")),
        "requires_key": requires_user_key(provider, final_base),   # 必须自备 Key？
        "has_env_key": bool(env_key),                             # 服务端有默认 Key 可共享？
        "is_local": is_local_llm(provider, final_base),
    }


def describe_error(err: str) -> str:
    """把底层异常翻译成一句人话，给「测试连通」按钮显示。
    注意：Windows 中文版系统返回的是中文错误（如「由于目标计算机积极拒绝」），
    所以中英文关键词都要匹配。"""
    s = (err or "").lower()
    raw = err or ""
    if not s:
        return "未知错误"
    if "401" in s or "unauthorized" in s or "invalid api key" in s or "incorrect api key" in s:
        return "鉴权失败：API Key 无效或未填写"
    if "403" in s:
        return "被拒绝(403)：Key 无权限，或被网关/Cloudflare 拦截"
    if "404" in s:
        return "地址不对(404)：base_url 需指向 OpenAI 兼容根路径（通常以 /v1 结尾）"
    if "429" in s or "rate limit" in s:
        return "触发限流(429)：稍后重试或换 Key"
    if "timed out" in s or "timeout" in s or "超时" in raw:
        return "连接超时：服务未启动、地址不通，或模型响应过慢"
    if "refused" in s or "拒绝" in raw:
        return "连接被拒：服务未启动，或端口/地址写错"
    if ("name or service not known" in s or "nodename nor servname" in s or "getaddrinfo" in s
            or "解析" in raw):
        return "域名解析失败：base_url 中的主机名写错或无法解析"
    if "certificate" in s or "ssl" in s or "证书" in raw:
        return "TLS/证书错误：https 证书不匹配或被中间设备拦截"
    if "returned abnormal" in s or "返回异常结构" in raw:
        return "返回结构异常：端点不是 OpenAI 兼容接口，或模型名不存在"
    if "502" in s or "bad gateway" in s:
        return "网关 502：目标不可达（若本机/服务端配了代理，可能是代理访问不到这个地址）"
    if "500" in s or "503" in s or "504" in s or "server error" in s:
        return "服务端故障(5xx)：模型服务临时不可用，稍后重试"
    return "连接失败（详情见下方英文原错）"


def server_default_key(provider: str) -> str:
    """服务端 .env 为某 provider 配置的默认 Key（即「管理员的 Key」来源）。
    只在【管理员本人】或【被管理员授权的用户】路径下才允许使用（见 app._resolve_llm_for_user）。"""
    cfg = PROVIDERS.get(provider, PROVIDERS["deepseek"])
    return os.getenv(cfg["default_api_key_env"], "").strip()


def is_own_gateway(base_url: str) -> bool:
    """base_url 是否指向用户自己的自建网关（如 qwen.dadaobo.com）—— 这类通常不校验 Key。"""
    u = (base_url or "").strip().lower()
    return bool(u) and any(h in u for h in _LOCAL_LLM_HOSTS)


def requires_user_key(provider: str, base_url: str = "") -> bool:
    """该 provider + 目标地址下，**是否必须由用户自备 API Key**。
    · ollama provider              → 不需要（本地服务一般不校验）
    · 本机/内网地址、用户自建网关   → 不需要（自己家的服务，通常不校验 Key）
    · 其余（deepseek、指向公网第三方的 openai_compat 等）→ 按 provider 的 needs_api_key 判定
    说明：只有「需要 Key」且用户既没填自己的、也没被授权共享时，才会被拦（2026-10-16）。"""
    p = (provider or "").strip().lower()
    if p == "ollama":
        return False
    if is_intranet_url(base_url) or is_own_gateway(base_url):
        return False
    cfg = PROVIDERS.get(provider, PROVIDERS["deepseek"])
    return bool(cfg.get("needs_api_key"))


def ping(provider: str = "deepseek", api_key: str = "", base_url: str = "",
         model: str = "", timeout: int = 30, allow_env_key: bool = True):
    """LLM 探活：用最小请求（max_tokens=8，"ping"）测试端点当前是否可用。
    返回 (ok: bool, err: str)。ok=True 表示可以正常生成；
    服务故障（5xx / 超时 / 连接失败）返回 (False, 错误摘要)；
    鉴权错误（401/403/无效 key）同样返回 False，由调用方识别后快速失败（等待无意义）。
    allow_env_key=False 时**绝不读服务端 .env 的 Key**（未获管理员授权的用户就是这种情况）。"""
    try:
        cfg = PROVIDERS.get(provider, PROVIDERS["deepseek"])
        env_model_var = cfg.get("default_model_env") or (provider.upper() + "_MODEL")
        final_model = (model or os.getenv(env_model_var, "") or cfg["default_model"]).strip()
        if provider == "openai_compat":
            final_base = (base_url or os.getenv("OPENAI_COMPAT_BASE_URL") or "").strip() or cfg["default_base_url"]
            # ⚠️ 传入的 api_key 优先（多租户：各人用各人的 Key），env 仅作兜底且可被禁用
            env_k = os.getenv("OPENAI_COMPAT_API_KEY") if allow_env_key else ""
            final_key2 = (api_key or env_k or "EMPTY").strip()
            _urllib_chat(final_base, final_key2, final_model,
                         [{"role": "user", "content": "ping"}],
                         temperature=0, max_tokens=8, timeout=timeout)
        else:
            client = _make_client(provider, api_key=api_key, base_url=base_url,
                                  allow_env_key=allow_env_key)
            # with_options：探活不重试 + 短超时，一次快速判定
            client = client.with_options(max_retries=0, timeout=timeout)
            client.chat.completions.create(
                model=final_model,
                messages=[{"role": "user", "content": "ping"}],
                max_tokens=8, temperature=0,
            )
        return True, ""
    except Exception as e:
        return False, str(e)[:200]


BASE_SYSTEM = """你是{product}的资深新媒体文案，专写"看不出是广告"的微信公众号爆款软文：第一人称、真实故事感，读者看完会想买。

【最高优先级】用户素材里若有【写作要求】【标题要求】【转化要求】【禁忌】等指令，必须逐条满足；与下面通用规则冲突时，一律以用户素材为准。

【一、标题 · 必须标新立异（决定打开率，最重要）】
- 16-26 字，像真实爆款标题，绝不平淡。禁止说明文式标题（"XX的三大好处""如何改善XX""XX是什么"）。
- 每篇标题换一种"公式"，同一批文章禁止重复句式。可选公式：
  ① 反差数字（"试了17种方法，最后靠一把梳子"）
  ② 悬念留白（"我妈塞给我一把梳子，三个月后她笑了"）
  ③ 身份代入（"30+打工人 / 新手妈妈 / 退休阿姨"）
  ④ 冲突打脸（"被同事笑了一整个月，直到她们来问我"）
  ⑤ 意外结果（"本来只想试试，结果全家抢着用"）
  ⑥ 具体场景（"凌晨三点，我又盯着天花板"）
  ⑦ 直接引语（"闺蜜说：你的头发比我奶奶还白"）
  ⑧ 代价损失（"为这个毛病我花了小一万"）
- 标题里必须出现具体元素（数字/时间/物品/称呼/地名），不要空泛形容词。

【二、结构与钩子（保证读完 + 想买）】
1. 开头 1-2 句必须是钩子：具体场景、冲突或悬念。禁止背景铺垫、禁止"我今年XX岁""说起XX"式平铺开头。
2. 前三分之一：把痛点写到"扎心"——具体到时间、动作、对话、旁人反应，让同类人自动代入。
3. 中段：写"试错清单"（越具体越真实，失败也要写透），再"偶然"遇到产品；禁止突然转推销。
4. 产品出场：用一次真实使用画面替代卖点罗列（手感/声音/气味/用了多久/当时在想什么），产品名自然出现 2-3 次即可。
5. 转折与结果：变化要克制真实（"不是立刻就好，三周后我发现自己……"），明确承认"不是药、起效没有那么快"。
6. 结尾（软钩子，强转化但不像广告）：用**有亲和力的邀请式**收尾——像熟人聊完天自然问一句，语气真诚、平等、带点"你的经验我也很想听"的味道。参考说法："你有什么好的经验愿意分享，欢迎留下你的宝贵信息""你家里要是有类似的小办法，也欢迎说给我听听""要是你在这方面有更好的心得，欢迎交流"。禁止命令式催互动（"评论区说说""赶紧留言""点赞转发"），禁止出现价格、链接、购买方式、二维码。

【三、去 AI 化 · 硬性清单】
- 全程第一人称口语，允许口头禅、半截话、自我纠正（"我一开始真不信——说实话现在也半信半疑"）。
- 必须有 3 处以上"只有真实经历才写得出的细节"：具体金额、具体时间、杂牌对比、某人的一句原话。
- 禁止：书面语连接词（首先/其次/总而言之/综上所述）、大段排比、连续四字成语、"不是…而是…"式 AI 腔、结尾总结升华。
- 禁止文言/古风腔调；产品素材里若有古籍典故，只用一两句白话带过，绝不照抄。

【四、篇幅】600-1000 字，正文空行分段。

【多篇差异化方向】按需选用：① 自己的痛经历（自救失败+重新摸索）② 试过的各种方法清单（最后发现该产品有用）③ 家人/送礼/办公室场景 ④ 对比他人（同事/邻居/亲戚的变化）。每篇只用一个主角度，绝不重叠。"""


# 兜底：未在产品库选产品时，默认注入"百秘一梳"品牌故事。
DEFAULT_PRODUCT_INTRO = """【产品背景素材·百秘一梳】天然桃木梳，取桃木为器、浸百草之液、藏养发精华，古法传承；晨昏勤梳可养发养身，通经活络。品牌故事源于汉末名医华佗一脉，历经千年传承至今。软文中可作为"天然材质/长辈相传/古法养生"的背书，务必自然、勿整段照搬典故。"""


def _make_messages(product: str, product_intro: str, topic: str, count: int,
                   extra_hint: str, brand: str, style_hint: str = "",
                   history_titles: Optional[List[str]] = None) -> List[Dict]:
    system = BASE_SYSTEM.format(product=product)
    # 保留旧 brand 字段用于替换兜底
    material = product_intro.strip() or DEFAULT_PRODUCT_INTRO
    # 产品库里的"写作要求" → 最高优先级注入
    req_block = ""
    if (style_hint or "").strip():
        req_block = f"""

【★写作要求 · 最高优先级，必须逐条满足（来自产品库设置）】
{style_hint.strip()}"""
    # 历史已发布标题 → 禁止雷同（防撞车）
    hist_block = ""
    htitles = [t.strip() for t in (history_titles or []) if (t or "").strip()]
    if htitles:
        lines = "\n".join(f"{i+1}. {t}" for i, t in enumerate(htitles[:30]))
        hist_block = f"""

【历史已发布标题 · 禁止雷同（最高优先级）】以下标题已经发过，你写的 {count} 个新标题必须与它们**完全不同**——
禁止相同句式、相同开头、相同数字、相同称呼、相同场景；也禁止只是换个词的"换皮"标题。每个标题都必须是全新的角度：
{lines}"""
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": f"""主题：{topic}

【产品素材】（其中典故/来历仅作背景，绝不照抄文言或堆砌）
{material}{req_block}{hist_block}

请严格按下面 JSON 结构返回（必须是对象，最外层一个 articles 数组，数组里有 {count} 个元素）：
{{
  "articles": [
    {{"title": "...", "body_md": "..."}},
    {{"title": "...", "body_md": "..."}},
    {{"title": "...", "body_md": "..."}}
  ]
}}

要求：
- 标题：按【一、标题·必须标新立异】的公式；{count} 篇各用一种不同公式，禁止重复句式，且必须含具体元素
- 内容：按【二、结构与钩子】；开头第一句即钩子，结尾用**有亲和力的邀请式**收尾（真诚邀请对方分享经验/心得，如"你有什么好的经验愿意分享，欢迎留下你的宝贵信息"），不要命令式催评论（不得出现价格/链接/购买方式）
- 去 AI：逐条执行【三、去 AI 化 · 硬性清单】
- {count} 篇角度差异明显，绝不重叠
- body_md 用空行分隔自然段，每篇 600-1000 字
- 软文中自然融入产品来历/卖点作背书，不整段照搬、不堆砌卖点，读起来像真事

【富排版要求·让文章好看不呆板】body_md 里按叙事需要穿插以下轻量 markdown 标记（别乱用，适可而止）：
- 段内想强调的句子用 **加粗**（每篇 2-4 处即可）
- 可用 > 引用写 1 句"过来人/长辈/感受"点睛之语（如：> 后来我才明白，…）
- 需分点列方法/清单时用 "- " 无序列表，别用大段排比
- 想在中间切节奏时用 --- 一行作分隔
- 若有小节可用 #### 小标题（每篇至多 2 个，别多用）
- 正文仍以自然口语叙事为主，以上标记是为了阅读有呼吸感，不是模板堆砌
{('额外提示：' + extra_hint) if extra_hint else ''}
"""},
    ]


# 单次 LLM 调用最多生成的篇数：一次生成过多篇时，总输出易超 max_tokens 被截断、
# JSON 不完整导致整批解析失败。count 较大时分批调用，批内失败自动降级逐篇，尽量保住能生成的文章。
_MAX_PER_CALL = 3

# ---- 本地 / 自建大模型：强制「一次只生成 1 篇」（2026-10-15 修正）----
# 背景：本地/自建模型（qwen.dadaobo.com 网关、局域网 llama.cpp/vLLM、Ollama）单篇就要十几秒起，
# 且输出 token 受限。一次要 2~3 篇 → 极易超 max_tokens 被截断、JSON 不完整，
# 或干脆超过 LLM_TIMEOUT 无响应（表现为「生成不出」）。故这类模型一律逐篇串行。
# 判定依据（满足任一即是）：provider 为 ollama/openai_compat；或 base_url 指向
# localhost / 内网 IP 段 / 自建域名（LOCAL_LLM_HOSTS，逗号分隔，默认含本项目 qwen 网关域名）。
_INTRANET_RE = re.compile(
    r"://(?:localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|"
    r"10\.\d{1,3}\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)",
    re.I)
_LOCAL_LLM_HOSTS = tuple(
    h.strip().lower()
    for h in os.getenv("LOCAL_LLM_HOSTS", "dadaobo.com").split(",")
    if h.strip())


def is_intranet_url(url: str) -> bool:
    """URL 是否指向本机 / 内网（这类地址必须直连，不能走系统代理 ——
    代理通常无法访问 127.0.0.1 / 192.168.x，会返回 502 等无用错误）。"""
    u = (url or "").strip().lower()
    return bool(u) and bool(_INTRANET_RE.search(u))


def is_local_llm(provider: str, base_url: str = "") -> bool:
    """是否本地 / 自建大模型（这类模型必须逐篇生成）。"""
    p = (provider or "").strip().lower()
    if p in ("ollama", "openai_compat"):
        return True
    u = (base_url or "").strip().lower()
    if not u:
        return False
    if _INTRANET_RE.search(u):
        return True
    return any(h in u for h in _LOCAL_LLM_HOSTS)


def plan_batches(count: int, provider: str, base_url: str = "") -> List[int]:
    """规划「每次 LLM 调用生成几篇」：
    · 本地/自建模型 → 每批 1 篇（逐篇串行，一篇一篇生成直到全部完成）
    · 云端模型     → 每批最多 _MAX_PER_CALL 篇（原行为，省请求）
    返回每批篇数列表，如 count=3 时 本地→[1,1,1]、云端→[3]；count=5 云端→[3,2]。"""
    max_per = 1 if is_local_llm(provider, base_url) else _MAX_PER_CALL
    sizes: List[int] = []
    remaining = max(int(count), 1)
    while remaining > 0:
        n = min(max_per, remaining)
        sizes.append(n)
        remaining -= n
    return sizes


def generate_articles(topic: str, count: int = 3, brand: str = "百秘一梳",
                      extra_hint: str = "",
                      provider: str = "deepseek", api_key: str = "",
                      base_url: str = "", model: str = "",
                      product_name: str = "", product_intro: str = "",
                      product_style: str = "",
                      history_titles: Optional[List[str]] = None,
                      allow_env_key: bool = True) -> List[Dict]:
    """
    按主题生成多篇差异化营销软文
    返回 [{"title":..., "body_md":...}, ...]

    product_name/product_intro：所选产品的名称与介绍（来自产品库）。未给时用 brand/默认品牌素材兜底。
    product_style：产品库里的"写作要求"（标题/钩子/人群/禁忌等），作为最高优先级注入提示词。
    history_titles：历史已发布标题列表 → 注入提示词禁止雷同（配合 dedup.py 后过滤层做两层防撞车）。
    分批策略（见 plan_batches）：
      · 本地/自建模型（ollama / openai_compat / 内网或自建域名）→ 每批 1 篇，逐篇串行生成，
        一次只让模型写一篇，避免「一次要 N 篇」导致响应不及时 / 超时 / 生成不出；
      · 云端模型（deepseek 等）→ 每批最多 _MAX_PER_CALL 篇，同时防止单次输出超长被截断。
    某批失败会自动降级为逐篇生成，尽量不因单次超长输出而丢掉整批。
    """
    out: List[Dict] = []
    errors: List[str] = []
    batch_sizes = plan_batches(count, provider, base_url)
    for bi, n in enumerate(batch_sizes):
        hint = extra_hint or ""
        if out:
            titles = "、".join(f"《{it['title']}》" for it in out[-6:])
            hint = (hint + "；" if hint else "") + f"上面已经写过：{titles}。本批必须换全新角度、标题与场景，绝不能与上面雷同。"
        try:
            batch = _gen_batch(topic, n, brand=brand, extra_hint=hint, provider=provider,
                               api_key=api_key, base_url=base_url, model=model,
                               product_name=product_name, product_intro=product_intro,
                               product_style=product_style, history_titles=history_titles,
                               allow_env_key=allow_env_key)
            out.extend(batch)
            log.info("生成批 %d/%d 成功 %d 篇（累计 %d/%d）", bi + 1, len(batch_sizes), len(batch), len(out), count)
        except Exception as e:
            log.warning("生成批 %d/%d（%d 篇）失败：%s → 降级逐篇生成…", bi + 1, len(batch_sizes), n, str(e)[:120])
            for _ in range(n):
                try:
                    one = _gen_batch(topic, 1, brand=brand, extra_hint=hint, provider=provider,
                                     api_key=api_key, base_url=base_url, model=model,
                                     product_name=product_name, product_intro=product_intro,
                                     product_style=product_style, history_titles=history_titles,
                                     allow_env_key=allow_env_key)
                    out.extend(one)
                except Exception as e2:
                    errors.append(str(e2)[:300])
                    log.error("单篇生成失败：%s", str(e2)[:160])
    if not out and errors:
        raise ValueError(f"所有文章生成失败（{len(errors)} 次尝试），最后错误：{errors[-1]}")
    if len(out) < count:
        log.warning("生成数不足：请求 %d 篇，实际生成 %d 篇", count, len(out))
    return out


def _gen_batch(topic: str, count: int = 3, brand: str = "百秘一梳",
               extra_hint: str = "",
               provider: str = "deepseek", api_key: str = "",
               base_url: str = "", model: str = "",
               product_name: str = "", product_intro: str = "",
               product_style: str = "",
               history_titles: Optional[List[str]] = None,
               allow_env_key: bool = True) -> List[Dict]:
    """单次 LLM 调用生成 count（≤ _MAX_PER_CALL）篇；解析失败抛 ValueError。"""
    cfg = PROVIDERS.get(provider, PROVIDERS["deepseek"])
    # 从 provider 专属 env 读 model（如 OPENAI_COMPAT_MODEL / DEEPSEEK_MODEL）
    env_model_var = cfg.get("default_model_env") or (provider.upper() + "_MODEL")
    final_model = (model or os.getenv(env_model_var, "") or cfg["default_model"]).strip()
    if not final_model:
        raise ValueError(f"provider={provider} 需要指定 model（如 qwen2.5:7b / llama3:8b）")

    client = _make_client(provider, api_key=api_key, base_url=base_url, allow_env_key=allow_env_key)
    client = client.with_options(timeout=LLM_TIMEOUT)   # 兜底超时：端点无响应时不无限阻塞
    log.info("LLM 调用: provider=%s model=%s product=%s", provider, final_model, product_name or brand)

    if product_name:
        messages = _make_messages(product=product_name, product_intro=product_intro, topic=topic,
                                  count=count, extra_hint=extra_hint, brand=brand,
                                  style_hint=product_style, history_titles=history_titles)
    else:
        # 兼容旧调用：用 brand 作为产品名，产品素材用默认品牌介绍
        messages = _make_messages(product=brand, product_intro="",
                                  topic=topic, count=count, extra_hint=extra_hint, brand=brand,
                                  history_titles=history_titles)
    # Ollama 本地模型对 response_format=json_object 支持不一，去掉它更稳
    kwargs = dict(model=final_model, messages=messages, temperature=0.85, max_tokens=8000)
    if provider != "ollama":
        try:
            kwargs["response_format"] = {"type": "json_object"}
        except Exception:
            pass

    if provider == "openai_compat":
        # 部分自建网关只放行 curl UA，openai SDK 默认 UA 会被 403/blocked。
        # 且模型多为 reasoning 型（如 llama.cpp 上的 Qwen3）：必须真正关闭思考，否则
        # 会把 token 全耗在 reasoning_content 上、content 为空 → 解析不到 JSON。
        # ⚠️ 关键：llama.cpp / 多数 OpenAI 兼容网关**只认 chat_template_kwargs 里的
        # enable_thinking**（顶层 enable_thinking 经常被忽略，导致一直思考不出正文）。
        final_base = (base_url or os.getenv("OPENAI_COMPAT_BASE_URL") or "").strip() or cfg["default_base_url"]
        # ⚠️ 传入的 api_key 优先（多租户：各人用各人的 Key），env 仅作兜底且可被禁用
        env_k = os.getenv("OPENAI_COMPAT_API_KEY") if allow_env_key else ""
        final_key2 = (api_key or env_k or "EMPTY").strip()
        text = _urllib_chat(final_base, final_key2, final_model, messages,
                            temperature=0.85,
                            max_tokens=6000,
                            extra_body={"chat_template_kwargs": {"enable_thinking": False}},
                            timeout=LLM_TIMEOUT)
    else:
        resp = client.chat.completions.create(**kwargs)
        _fr = (resp.choices[0].finish_reason or "") if resp.choices else ""
        text = (resp.choices[0].message.content or "").strip()
        if _fr and _fr != "stop":
            log.warning("LLM finish_reason=%s（返回 %d 字），输出可能被截断", _fr, len(text))
    log.info("LLM 返回 %d 字", len(text))

    # 尝试直接 JSON 解析；解析失败时从文本里抠 JSON 块
    data = _parse_json_lenient(text)

    articles = []
    if isinstance(data, dict):
        for k in ("articles", "items", "list", "data"):
            v = data.get(k)
            if isinstance(v, list) and v:
                articles = v
                break
        if not articles and "title" in data:
            articles = [data]
    elif isinstance(data, list):
        articles = data
    out = []
    for item in articles[:count]:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": (item.get("title") or "").strip(),
            "body_md": (item.get("body_md") or item.get("body") or "").strip(),
        })
    return out


def _parse_json_lenient(text: str):
    """尝试 JSON 解析；失败时从 markdown 代码块里抠。"""
    # 直接解析
    try:
        return json.loads(text)
    except Exception:
        pass
    # 抠 ```json ... ``` 或 ``` ... ```
    import re
    m = re.search(r"```(?:json)?\s*([\s\S]+?)\s*```", text)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 抠第一个 { 到最后一个 }
    s, e = text.find("{"), text.rfind("}")
    if s >= 0 and e > s:
        try:
            return json.loads(text[s:e + 1])
        except Exception:
            pass
    # ===== 兜底：LLM 输出被截断时，逐个提取已闭合的完整 article 对象 =====
    partial = _try_extract_partial_articles(text)
    if partial is not None:
        return partial
    raise ValueError(f"LLM 返回无法解析为 JSON（共 {len(text)} 字）：\n开头: {text[:160]}\n结尾: {text[-260:]}")


def _try_extract_partial_articles(text: str):
    """容错：当 LLM 返回的 JSON 因超长被截断时，提取数组中每一个已完整闭合的 article 对象。
    返回 {articles:[...]}（仅含能独立 json 解析成功的前缀完整项），无法提取则 None。"""
    import json as _j, re as _re
    # 定位顶层 "articles" 数组起点
    arr_start = _re.search(r'"articles"\s*:\s*\[', text)
    if not arr_start:
        # 若整体本身是数组开头
        if text.lstrip().startswith("["):
            arr_start = _re.search(r"\[", text)
        else:
            return None
    idx = arr_start.end()
    items = []
    depth = 0
    i = idx
    in_str = False
    esc = False
    obj_buf = ""        # 累积当前对象内容
    obj_started = False
    n = len(text)
    while i < n:
        ch = text[i]
        if in_str:
            obj_buf += ch
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        else:
            if ch == '"':
                in_str = True; obj_buf += ch
            elif ch == '{':
                depth += 1
                if depth == 1:
                    obj_started = True
                obj_buf += ch
            elif ch == '}':
                depth -= 1
                obj_buf += ch
                if depth == 0 and obj_started:
                    # 一个完整对象闭合，尝试解析
                    try:
                        item = _j.loads(obj_buf)
                        if isinstance(item, dict) and item.get("title"):
                            items.append(item)
                        obj_buf = ""; obj_started = False
                    except Exception:
                        # 该对象不完整/出错，停止（通常是被截断处）
                        break
            elif ch == ',' and depth == 0:
                obj_buf = ""  # 数组元素分隔（对象间逗号）重置（对象本身已处理）
            else:
                obj_buf += ch
        i += 1
    if items:
        return {"articles": items}
    return None


if __name__ == "__main__":
    import sys
    topic = sys.argv[1] if len(sys.argv) > 1 else "偏头痛"
    provider = os.getenv("LLM_PROVIDER", "deepseek")
    model = os.getenv("LLM_MODEL", "")
    result = generate_articles(topic, 3, provider=provider, model=model)
    print(json.dumps(result, ensure_ascii=False, indent=2))