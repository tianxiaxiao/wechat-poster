"""
微信卡片排版渲染器 — 复刻「多平台图文生产系统」(md2card) 的公众号模式生成逻辑
================================================================================
调用 app/wxcard/render.js（Node + jsdom）执行 md2card 的「复制到公众号」管线：
  markdown → 卡片结构 → 主题 CSS 按选择器内联 → var()/calc() 解析
  → 微信兼容化(div/blockquote→section 等) → 背景拆 longhand + 渐变纯色兜底

产出可直接放入草稿 content_html 的富排版 HTML（与在 md2card 编辑器里
「公众号模式 → 复制到公众号」粘贴到微信编辑器的效果一致）。

可选主题（md2card 官方 5 套公众号专用样式）：
  bytedance 字节范 / alibaba 阿里橙(默认) / notebook 笔记本
  business 商务简报 / japanese-magazine 日本杂志
"""
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path

logger = logging.getLogger("poster.wxcard")

_DIR = Path(__file__).parent
_RENDER_JS = _DIR / "wxcard" / "render.js"
# node 根目录（托管版）；具体版本号会变（22.22.2-3 → -6），所以必须动态发现，
# 不能写死 —— 2026-10-06 就因托管 node 换版本导致渲染整批静默降级。
_NODE_ROOT = Path(r"C:\Users\Administrator\.workbuddy\binaries\node")
_node_cache = None


def find_node() -> Path:
    """动态定位 node.exe：环境变量 WX_NODE > versions/current > 版本号最高的 > PATH 里的 node"""
    global _node_cache
    if _node_cache and _node_cache.exists():
        return _node_cache
    cands = []
    env = (os.getenv("WX_NODE") or "").strip()
    if env:
        cands.append(Path(env))
    cands.append(_NODE_ROOT / "versions" / "current" / "node.exe")
    vers_dir = _NODE_ROOT / "versions"
    if vers_dir.exists():
        def _vkey(p: Path):
            digits = [int(x) for x in p.name.replace("-", ".").split(".") if x.isdigit()]
            return digits
        for d in sorted(vers_dir.iterdir(), key=_vkey, reverse=True):
            if d.is_dir():
                cands.append(d / "node.exe")
    for c in cands:
        try:
            if c.exists():
                _node_cache = c
                return c
        except Exception:
            continue
    # 最后退回 PATH 中的 node
    import shutil
    which = shutil.which("node")
    if which:
        _node_cache = Path(which)
        return _node_cache
    raise RuntimeError("找不到可用的 node.exe（可设置环境变量 WX_NODE 指定）")

THEMES = {"bytedance": "字节范", "alibaba": "阿里橙", "notebook": "笔记本",
          "business": "商务简报", "japanese-magazine": "日本杂志"}
DEFAULT_THEME = "alibaba"
# 自动发布随机池：排除「日本杂志」（2026-09-24 按用户要求）
PUBLISH_THEMES = ["bytedance", "alibaba", "notebook", "business"]


def list_themes() -> list:
    return [{"id": k, "name": v} for k, v in THEMES.items()]


def pick_theme() -> str:
    """自动发布时随机选一个主题（不含日本杂志）"""
    import random
    return random.choice(PUBLISH_THEMES)


def resolve_theme(theme: str = "") -> str:
    """空 / random / 非法值 → 随机池里挑一个；合法值原样返回"""
    t = (theme or "").strip()
    if t in THEMES:
        return t
    return pick_theme()


def render_md_with_theme(md: str, theme: str = "") -> tuple:
    """渲染并返回 (html, theme_id, theme_name)。theme 为空即随机（4 选 1，无日本杂志）"""
    tid = resolve_theme(theme)
    html = render_md_to_card_html(md, tid)
    return html, tid, THEMES[tid]


def render_md_to_card_html(md: str, theme: str = "") -> str:
    """markdown → 微信卡片富排版 HTML（失败抛异常，由调用方决定降级）。
    theme 留空 = 自动发布随机选（4 个主题，不含日本杂志）；非法值回退默认主题。"""
    node = find_node()
    theme = (theme or "").strip()
    if theme not in THEMES:
        theme = DEFAULT_THEME
    with tempfile.TemporaryDirectory() as td:
        in_path = Path(td) / "in.md"
        out_path = Path(td) / "out.html"
        in_path.write_text(md or "", encoding="utf-8")
        proc = subprocess.run(
            [str(node), str(_RENDER_JS), str(in_path), str(out_path), theme],
            capture_output=True, text=True, timeout=120,
            cwd=str(_DIR / "wxcard"),
            encoding="utf-8", errors="ignore",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"wxcard 渲染失败: {(proc.stderr or proc.stdout)[:300]}")
        try:
            info = json.loads(proc.stdout.strip().splitlines()[-1])
        except Exception:
            info = {}
        if not out_path.exists() or out_path.stat().st_size == 0:
            raise RuntimeError("wxcard 渲染无输出")
        html = out_path.read_text(encoding="utf-8")
        logger.info("wxcard 渲染完成: theme=%s bytes=%s", theme, info.get("bytes"))
        return html


def render_md_with_theme_safe(md: str, theme: str = "") -> tuple:
    """渲染并返回 (html, theme_id, theme_name)；失败降级旧版排版（theme_id 为空）。
    自动发布统一用这个：既拿到实际用的主题名（写进任务日志），又不会因渲染失败中断发布。"""
    try:
        html, tid, tname = render_md_with_theme(md, theme)
        if html and "<section" in html:
            return html, tid, tname
        raise RuntimeError("输出为空")
    except Exception as e:
        logger.warning("卡片渲染失败，降级旧版排版：%s", str(e)[:160])
        import wechat_api
        return wechat_api.md_to_html(md), "", "旧版排版"


def render_md_safe(md: str, theme: str = "") -> str:
    """卡片渲染（自动发布用）：theme 留空 → 随机选 4 个主题之一（无日本杂志）；
    失败时降级回旧版 md_to_html，保证发布流程不中断。"""
    return render_md_with_theme_safe(md, theme)[0]
