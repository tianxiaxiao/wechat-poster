"""
微信发布器 — 复用 wechat/pub_one.js（已走通的 playwright 流程）
通过 spawn Node 子进程调用，二维码写在 data/qrcode/<job_id>_<seq>.png
"""
import os
import json
import subprocess
import shutil
from pathlib import Path
from typing import Optional, Dict
import time

SCRIPT_DIR = Path(__file__).parent
WX_DIR = SCRIPT_DIR.parent / "wechat"
NODE_SCRIPT = SCRIPT_DIR / "publisher.js"  # 改造后的 Node 脚本，接受 args
COVER_SRC_DIR = WX_DIR / "covers"
QR_DIR = SCRIPT_DIR / "data" / "qrcodes"
LOG_DIR = SCRIPT_DIR / "data" / "logs"
QR_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)


def _pick_node() -> str:
    """跨机器探测 node 可执行文件（不写死用户名/版本号）。
    顺序：环境变量 NODE_EXE > PATH 中的 node > WorkBuddy 托管版本 > 系统安装目录。"""
    env_exe = os.environ.get("NODE_EXE")
    if env_exe and Path(env_exe).exists():
        return env_exe
    found = shutil.which("node")
    if found:
        return found
    home = Path.home()
    versions_dir = home / ".workbuddy" / "binaries" / "node" / "versions"
    try:
        cands = sorted([p / "node.exe" for p in versions_dir.iterdir()], reverse=True)
        cands += sorted([p / "bin" / "node" for p in versions_dir.iterdir()], reverse=True)
        for c in cands:
            if c.exists():
                return str(c)
    except Exception:
        pass
    for p in (r"C:\Program Files\nodejs\node.exe", "/usr/bin/node", "/usr/local/bin/node"):
        if Path(p).exists():
            return p
    return ""


def _pick_node_path() -> str:
    """跨机器组装 NODE_PATH（playwright-core 的安装位置可能不同）。"""
    parts = []
    env_np = os.environ.get("NODE_PATH")
    if env_np:
        parts.append(env_np)
    home = Path.home()
    parts.append(str(SCRIPT_DIR / "node_modules"))     # 项目内安装
    parts.append(str(home / "node_modules"))           # 用户目录安装（历史做法）
    return os.pathsep.join(parts)


def pick_random_cover() -> Optional[Path]:
    """从素材库 covers/ 随机选一张封面"""
    if not COVER_SRC_DIR.exists():
        return None
    covers = [p for p in COVER_SRC_DIR.iterdir() if p.suffix.lower() in (".png", ".jpg", ".jpeg")]
    if not covers:
        return None
    import random
    return random.choice(covers)


def publish_one(title: str, body_md_path: Path, author: str,
                cover_path: Path, qrcode_path: Path, log_path: Path,
                publish_id_path: Path, save_draft_only: bool = False,
                original: bool = True, reward: bool = True) -> Dict:
    """
    调 Node 子进程走完一篇发布流程
    返回 {"ok": bool, "publish_id": str|None, "log": str}

    save_draft_only=True：只走"填字段+勾原创+勾赞赏+保存草稿"，不点发表/不弹码。
        返回 {"ok": True, "saved": True, "publish_id": "SAVED"}；调用方需再用 wechat_api 发布。
    original / reward：是否声明原创 / 开启赞赏（默认开启；仅 save_draft_only 或扫码流程生效，
        纯 API draft/add 路径微信不支持这两项，需走本 playwright 流程才生效）。
    """
    # 选 node 路径（可移植：环境变量 > PATH > WorkBuddy 托管版本 > 系统安装）
    node_exe = _pick_node()
    if not node_exe:
        node_exe = "node"

    cmd = [
        node_exe,
        str(NODE_SCRIPT),
        "--title", title,
        "--author", author,
        "--body", str(body_md_path),
        "--cover", str(cover_path),
        "--qrcode", str(qrcode_path),
        "--publish-id-out", str(publish_id_path),
        "--log", str(log_path),
    ]
    if save_draft_only:
        cmd += ["--save-draft-only", "true"]
    # 原创/赞赏：是否勾选（默认勾选）；微信纯 API 不支持，只有走本 playwright UI 流程才生效
    cmd += ["--original", "true" if original else "false",
            "--reward", "true" if reward else "false"]
    env = os.environ.copy()
    # playwright-core 可能装在用户目录 node_modules 或项目内，两者都放进 NODE_PATH（可移植）
    env["NODE_PATH"] = _pick_node_path()

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=420, env=env)
        log_text = proc.stdout + "\n[STDERR]\n" + proc.stderr
        log_path.write_text(log_text, encoding="utf-8")

        # 解析 publish_id / 成功标志
        pid = None
        if publish_id_path.exists():
            pid = publish_id_path.read_text(encoding="utf-8").strip()
        # 兼容：PUBLISH_OK 为发布成功的标志；SAVED 为仅保存草稿(主动 save_draft_only)；
        # SAVED_DRAFT 为扫码确认后微信只存了草稿未真正发表；空表示失败
        draft_only = pid == "SAVED_DRAFT"
        ok = proc.returncode == 0 and pid in ("PUBLISH_OK", "SAVED") and not draft_only
        return {
            "ok": ok,
            "publish_id": pid or None,
            "saved": pid == "SAVED" or draft_only,
            "draft_saved": draft_only,
            "log": log_text,
            "returncode": proc.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "publish_id": None, "log": "TIMEOUT after 420s", "returncode": -1}
    except Exception as e:
        return {"ok": False, "publish_id": None, "log": str(e), "returncode": -1}


def get_current_qrcode_path(job_id: str, seq: int) -> Path:
    return QR_DIR / f"{job_id}_{seq}.png"
