#!/usr/bin/env python3
"""公众号自动发布服务 —— 轻量进程守护（纯标准库，零依赖）
================================================================================
解决什么问题：
  工具本身是个前台进程（`uvicorn`），终端一关 / 会话结束就没了。本脚本负责
  「端口没在监听就把服务拉起来」，可循环守护。

什么时候需要它：
  · **Windows**：没有内置的服务管理器 → 需要本脚本（配合「启动」文件夹或任务计划）。
  · **Linux**：用 systemd（`Restart=always`）就够了，见 `deploy/linux/`，**不需要**本脚本。
  · **macOS**：用 launchd（`KeepAlive`）就够了，见 `deploy/macos/`，**不需要**本脚本。
  · **Docker / 群晖 NAS**：compose 里已配 `restart: unless-stopped` + 镜像自带 HEALTHCHECK，
    **不需要**本脚本。

用法：
  python scripts/guard.py --once                  # 检查一次，端口不通就拉起，然后退出
  python scripts/guard.py --supervise             # 常驻守护（默认每 300s 检查一次）
  python scripts/guard.py --supervise --interval 60
  python scripts/guard.py --dry-run               # 只打印将要使用的解释器/命令/日志路径，不启动任何东西
  python scripts/guard.py --port 8899 --host 0.0.0.0

可选环境变量：
  POSTER_PYTHON           指定 Python 解释器（默认自动发现项目内 .venv / venv，再退回当前解释器）
  POSTER_GUARD_LOG_DIR    日志目录（默认 <项目根>/logs）

日志：
  <项目根>/logs/guard.log   守护自身日志
  <项目根>/logs/server.log  服务输出（uvicorn 的 stdout/stderr）
  ⚠️ 两个文件都是 UTF-8；Windows 上用记事本/VS Code 打开即可，别用老式 GBK 终端直接 type。
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_INTERVAL = 300
LOCK_TTL = 120          # 秒：防止两个触发同时拉起两份服务
APP_SUBDIR = "app"      # uvicorn 的工作目录（app:app 所在位置）
LOGS_SUBDIR = "logs"


# ---------------------------------------------------------------- 路径解析
def project_root() -> Path:
    """项目根 = 本文件所在目录的上一级（scripts/guard.py → 项目根）。"""
    return Path(__file__).resolve().parent.parent


def app_dir(root: Path | None = None) -> Path:
    """uvicorn 的工作目录：<项目根>/app（`app:app` 模块就在这里）。"""
    return (root or project_root()) / APP_SUBDIR


def log_dir(root: Path | None = None) -> Path:
    """日志目录，可用 POSTER_GUARD_LOG_DIR 覆盖。"""
    env = os.getenv("POSTER_GUARD_LOG_DIR", "").strip()
    return Path(env) if env else (root or project_root()) / LOGS_SUBDIR


def resolve_python(root: Path | None = None) -> str:
    """找一个能跑本项目的 Python 解释器：
    POSTER_PYTHON > 项目内 .venv / venv > 当前解释器。全部按相对路径推导，不含任何硬编码绝对路径。"""
    env = os.getenv("POSTER_PYTHON", "").strip()
    if env and Path(env).exists():
        return env
    base = root or project_root()
    win = os.name == "nt"
    for venv in (".venv", "venv", "env"):
        cand = (base / venv / ("Scripts/python.exe" if win else "bin/python"))
        if cand.exists():
            return str(cand)
    return sys.executable or "python"


def build_cmd(python_exe: str, host: str, port: int) -> list:
    """要执行的命令（绝对路径由上面的函数推导，便于 --dry-run 检查与单元测试）。"""
    return [python_exe, "-m", "uvicorn", "app:app", "--host", host, "--port", str(port)]


# ---------------------------------------------------------------- 基础设施
def _say(msg: str):
    """往控制台打一行（Windows 老终端是 GBK，编码失败也绝不让守护崩掉）。"""
    try:
        print(msg, flush=True)
    except Exception:  # noqa: BLE001
        try:
            sys.stdout.buffer.write((msg + "\n").encode("utf-8", "replace"))
            sys.stdout.flush()
        except Exception:
            pass


def log(msg: str, root: Path | None = None):
    d = log_dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        with open(d / "guard.log", "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:  # noqa: BLE001
        pass
    _say(msg)


def port_open(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def _recent_lock(root: Path | None = None) -> bool:
    lock = log_dir(root) / "guard.lock"
    try:
        return lock.exists() and (time.time() - lock.stat().st_mtime) < LOCK_TTL
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------- 核心动作
def start_service(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
                  root: Path | None = None, quiet: bool = False,
                  wait_secs: int = 40) -> int:
    """端口没监听时拉起服务；返回 0 成功 / 1 失败。"""
    root = root or project_root()
    adir = app_dir(root)
    py = resolve_python(root)
    if not adir.exists():
        log(f"❌ 找不到应用目录：{adir}", root)
        return 1
    if not Path(py).exists() and py != sys.executable:
        log(f"❌ 找不到 Python 解释器：{py}", root)
        return 1
    d = log_dir(root)
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "guard.lock").write_text(str(time.time()), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    if not quiet:
        log(f"检测到 {host}:{port} 未监听 → 拉起服务…", root)
    flags = 0
    if os.name == "nt":
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        with open(d / "server.log", "ab") as out:
            subprocess.Popen(
                build_cmd(py, host, port), cwd=str(adir),
                stdout=out, stderr=subprocess.STDOUT,
                creationflags=flags, close_fds=True,
            )
    except Exception as e:  # noqa: BLE001
        log(f"❌ 启动失败：{e}", root)
        return 1
    waited = 0
    while waited < wait_secs:
        time.sleep(2)
        waited += 2
        if port_open(host, port):
            log(f"✅ 服务已启动并监听 {port}", root)
            return 0
    log(f"⚠️ 已发起启动，但 {wait_secs} 秒内端口仍未监听（详见 {d / 'server.log'}）", root)
    return 1


def check_once(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, root: Path | None = None) -> int:
    """单次检查：服务在跑就静默返回；否则拉起。"""
    if port_open(host, port):
        return 0
    if _recent_lock(root):
        log(f"{port} 未监听，但存在近期启动锁，跳过本次（等待上次启动生效）", root)
        return 0
    return start_service(host, port, root)


def supervise(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT,
              interval: int = DEFAULT_INTERVAL, root: Path | None = None) -> int:
    """常驻守护：每 interval 秒检查一次，服务掉了自动拉起。"""
    log(f"🛡 常驻守护启动（每 {interval}s 检查 {host}:{port}，PID={os.getpid()}）", root)
    fails = 0
    while True:
        try:
            if not port_open(host, port):
                rc = start_service(host, port, root)
                fails = fails + 1 if rc else 0
                if fails >= 3:
                    log("⚠️ 连续 3 次拉起失败 → 等 10 分钟再试（请看 logs/server.log）", root)
                    time.sleep(600)
                    fails = 0
        except KeyboardInterrupt:
            log("收到中断，守护退出", root)
            return 0
        except Exception as e:  # noqa: BLE001
            log(f"守护循环异常（继续运行）：{e}", root)
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log("收到中断，守护退出", root)
            return 0


# ---------------------------------------------------------------- 入口
def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="公众号自动发布服务 · 轻量进程守护（纯标准库）")
    p.add_argument("--host", default=DEFAULT_HOST, help=f"监听地址（默认 {DEFAULT_HOST}）")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"监听端口（默认 {DEFAULT_PORT}）")
    p.add_argument("--supervise", "-s", action="store_true", help="常驻守护（循环检查）")
    p.add_argument("--interval", type=int, default=DEFAULT_INTERVAL,
                   help=f"常驻守护的检查间隔秒数（默认 {DEFAULT_INTERVAL}，最小 30）")
    p.add_argument("--once", action="store_true", help="只检查一次（默认行为）")
    p.add_argument("--dry-run", action="store_true", help="只打印将要使用的解释器/命令/日志路径，不启动任何东西")
    args = p.parse_args(argv)

    root = project_root()
    d = log_dir(root)
    if args.dry_run:
        _say(f"项目根     : {root}")
        _say(f"应用目录   : {app_dir(root)}  (存在={app_dir(root).exists()})")
        _say(f"Python     : {resolve_python(root)}")
        _say(f"日志目录   : {d}")
        _say(f"启动命令   : {' '.join(build_cmd(resolve_python(root), args.host, args.port))}")
        _say(f"目标端口   : {args.host}:{args.port}  (当前监听={port_open(args.host, args.port)})")
        _say(f"守护模式   : {'supervise(每 %ds)' % max(30, args.interval) if args.supervise else 'once'}")
        return 0
    if not app_dir(root).exists():
        log(f"❌ 请在项目根目录下运行（找不到 {app_dir(root)}）。"
            f"若把本脚本移出了 scripts/，请用 POSTER_PYTHON / 手动指定路径。", root)
        return 1
    if args.supervise:
        return supervise(args.host, args.port, max(30, args.interval), root)
    return check_once(args.host, args.port, root)


if __name__ == "__main__":
    sys.exit(main())
