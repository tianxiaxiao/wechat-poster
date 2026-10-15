"""
install.py — 在新电脑上安装「润致公众号自动发布工具」。

用法（把 zip 解压到任意目录后，在该目录执行）：
    python install.py

它会：
  1. 检查 Python 版本，创建虚拟环境 .venv 并安装依赖
  2. 检查 Node.js，安装 playwright-core（扫码发表功能需要）
  3. 检测浏览器（Chrome/Edge），供扫码发表使用
  4. 解压微信登录态（若同目录有 wx-profile.zip）
  5. 生成一键启动脚本 start.bat（Windows）/ start.sh
"""
import os
import platform
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = Path(__file__).resolve().parent
HOME = Path.home()
IS_WIN = platform.system() == "Windows"
VENV = HERE / ".venv"
PIP_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"


def say(msg=""):
    print(msg, flush=True)


def step(n, total, title):
    say()
    say(f"[{n}/{total}] {title}")
    say("-" * 56)


def run(cmd, **kw):
    say(f"    $ {' '.join(str(c) for c in cmd)}")
    return subprocess.run(cmd, **kw)


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def setup_python():
    if sys.version_info < (3, 9):
        say(f"❌ 需要 Python 3.9 及以上，当前 {sys.version.split()[0]}")
        return False
    if not venv_python().exists():
        say("    创建虚拟环境 .venv …")
        if run([sys.executable, "-m", "venv", str(VENV)]).returncode != 0:
            say("❌ 创建虚拟环境失败")
            return False
    req = HERE / "docker" / "requirements.txt"
    if not req.exists():
        say(f"❌ 未找到依赖清单 {req}")
        return False
    py = str(venv_python())
    run([py, "-m", "pip", "install", "--upgrade", "pip", "-i", PIP_MIRROR, "-q"])
    r = run([py, "-m", "pip", "install", "-r", str(req), "-i", PIP_MIRROR])
    if r.returncode != 0:
        say("⚠️ 依赖安装失败，请检查网络后重试（可加代理或换镜像源）")
        return False
    say("    ✅ Python 依赖安装完成")
    return True


def setup_node():
    npm = shutil.which("npm") or shutil.which("npm.cmd")
    if not npm:
        say("    ⚠️ 未检测到 Node.js/npm —— 扫码发表功能将不可用（API 自动发布不受影响）")
        say("       如需扫码发表：安装 Node.js 后在本目录重新执行 python install.py")
        return False
    say("    安装 playwright-core 到 app/node_modules …")
    r = run([npm, "install", "--prefix", str(HERE / "app"), "playwright-core",
             "--registry", "https://registry.npmmirror.com", "--no-audit", "--no-fund"],
            shell=IS_WIN)
    if r.returncode != 0:
        say("    ⚠️ playwright-core 安装失败，扫码发表可能不可用")
        return False
    say("    ✅ playwright-core 安装完成")
    return True


def check_browser():
    cands = [
        Path(os.environ.get("CHROME_EXE", "")) if os.environ.get("CHROME_EXE") else None,
        HOME / ".agent-browser" / "browsers",
        Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
        Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
        Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
        Path("/usr/bin/google-chrome"),
        Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    ]
    if cands[1] and cands[1].is_dir():
        say("    ✅ 找到 WorkBuddy 自带浏览器（agent-browser）")
        return True
    for c in cands[2:]:
        if c and c.exists():
            say(f"    ✅ 找到浏览器: {c}")
            return True
    say("    ⚠️ 未找到 Chrome/Edge —— 扫码发表需要它。请安装 Chrome，")
    say("       或设置环境变量 CHROME_EXE 指向浏览器可执行文件。")
    return False


def setup_profile():
    zp = HERE / "wx-profile.zip"
    if not zp.exists():
        say("    （未提供 wx-profile.zip，跳过）")
        say("    首次使用扫码发表时，在弹出的浏览器窗口里手动登录一次微信公众平台即可。")
        return
    dst = HOME / ".workbuddy" / "wx-profile"
    dst.mkdir(parents=True, exist_ok=True)
    say(f"    解压登录态到 {dst} …")
    with zipfile.ZipFile(zp) as zf:
        for m in zf.namelist():
            if not m.startswith("wx-profile/"):
                continue
            rel = m[len("wx-profile/"):]
            if not rel:
                continue
            target = dst / rel
            if m.endswith("/"):
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(target, "wb") as f:
                    shutil.copyfileobj(src, f)
    for lock in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        try:
            (dst / lock).unlink(missing_ok=True)
        except Exception:
            pass
    say("    ✅ 登录态已还原（若微信提示需重新验证，手动扫码登录一次即可）")


def make_start_scripts():
    py = venv_python()
    if IS_WIN:
        bat = HERE / "start.bat"
        bat.write_text(
            "@echo off\r\n"
            "cd /d \"%~dp0app\"\r\n"
            "\"%~dp0.venv\\Scripts\\python.exe\" -m uvicorn app:app --host 127.0.0.1 --port 8765\r\n",
            encoding="ascii")
        say(f"    ✅ 已生成 {bat.name}（双击即可启动）")
    sh = HERE / "start.sh"
    sh.write_text(
        "#!/usr/bin/env bash\n"
        "cd \"$(dirname \"$0\")/app\"\n"
        "\"$(dirname \"$0\")/.venv/bin/python\" -m uvicorn app:app --host 127.0.0.1 --port 8765\n",
        encoding="utf-8")
    try:
        sh.chmod(0o755)
    except Exception:
        pass
    say(f"    ✅ 已生成 {sh.name}")


def main():
    say("=" * 56)
    say("润致公众号自动发布工具 · 新电脑安装")
    say(f"安装目录: {HERE}")
    say(f"系统: {platform.system()} {platform.release()} · Python {sys.version.split()[0]}")
    say("=" * 56)

    steps = [
        ("安装 Python 依赖", setup_python),
        ("安装浏览器自动化依赖（Node）", setup_node),
        ("检测浏览器", check_browser),
        ("还原微信登录态（可选）", setup_profile),
        ("生成启动脚本", make_start_scripts),
    ]
    for i, (title, fn) in enumerate(steps, 1):
        step(i, len(steps), title)
        try:
            fn()
        except Exception as e:
            say(f"    ⚠️ 该步骤出错（不阻断）：{type(e).__name__}: {e}")

    say()
    say("=" * 56)
    say("安装完成 🎉")
    say("=" * 56)
    say("启动方式：")
    if IS_WIN:
        say("    双击 start.bat （或双击后最小化窗口）")
    else:
        say("    ./start.sh")
    say("然后浏览器打开：http://127.0.0.1:8765")
    say()
    say("⚠️ 重要：若多台电脑同时使用同一个公众号，请只在其中一台开启「每日定时」，")
    say("   否则会出现重复发文（程序已做跨设备防重兜底，但仍建议单台启用定时）。")


if __name__ == "__main__":
    main()
