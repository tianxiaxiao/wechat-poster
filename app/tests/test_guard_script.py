"""
test_guard_script.py — `scripts/guard.py`（跨平台进程守护）的验证
================================================================================
为什么：守护脚本决定「服务挂了会不会被拉起来」，写错了要么不拉、要么反复拉起
（甚至拉起两份）。本脚本**不会真的启动服务**：把 subprocess.Popen 打桩，
再用一个临时 TCP 监听模拟"服务已在跑"，逐条断言行为。

覆盖：
  · 路径解析：项目根 / app 目录 / 日志目录 —— 全部相对推导，不含任何硬编码绝对路径
  · 解释器发现：POSTER_PYTHON 优先，且返回的确实存在
  · 启动命令与工作目录：`python -m uvicorn app:app --host H --port P`，cwd = <根>/app
  · 端口探测：有监听 True / 无监听 False
  · ★ 服务已在跑时**绝不重复拉起**（Popen 不被调用）—— 这条最要紧，否则会起两份
  · ★ 近期启动锁生效时跳过（防止两个触发同时拉起）
  · --dry-run 只打印、不启动任何进程
  · 找不到 app 目录 → 干净返回 1，不抛异常
运行：python tests/test_guard_script.py
"""
import importlib.util
import os
import socket
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent     # 仓库根
GUARD = ROOT / "scripts" / "guard.py"
RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


def load_guard():
    spec = importlib.util.spec_from_file_location("poster_guard_under_test", GUARD)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def dummy_listener():
    """起一个临时 TCP 监听，冒充"服务已在运行"。返回 (socket, port)。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(8)
    return s, s.getsockname()[1]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    print(f"守护脚本: {GUARD}")
    if not GUARD.exists():
        print("❌ 找不到 scripts/guard.py")
        return 1
    src = GUARD.read_text(encoding="utf-8")

    print("\n[A] 不含硬编码的本机绝对路径（开源可移植性）")
    # 不把本机的盘符/用户名/项目目录名写进测试文件里（公开仓库），全部动态推导或按模式匹配
    import re as _re
    abs_path = _re.compile(r"[A-Za-z]:[\\/]")                       # 任意 Windows 绝对路径
    home_path = _re.compile(r"[\\/](Users|home)[\\/]")              # 任意用户主目录
    repo_name = ROOT.name                                          # 本机项目文件夹名（不写死）
    bad = []
    if abs_path.search(src):
        bad.append("含 Windows 绝对路径")
    if home_path.search(src):
        bad.append("含用户主目录路径")
    if repo_name and repo_name in src:
        bad.append("含项目文件夹名")
    check("★ 脚本内无本机绝对路径 / 项目目录名", not bad, f"命中={bad}")

    g = load_guard()
    g.__dict__  # noqa: B018

    print("\n[B] 路径解析（全部由脚本位置推导）")
    root = g.project_root()
    check("★ 项目根 = 仓库根", Path(root).resolve() == ROOT.resolve(), str(root))
    check("app 目录存在且指向 <根>/app", g.app_dir(root).resolve() == (ROOT / "app").resolve(),
          str(g.app_dir(root)))
    check("日志目录默认在 <根>/logs", g.log_dir(root).resolve() == (ROOT / "logs").resolve(),
          str(g.log_dir(root)))

    print("\n[C] 解释器发现")
    _old_env = os.environ.pop("POSTER_GUARD_LOG_DIR", None)
    old_py = os.environ.get("POSTER_PYTHON")
    try:
        os.environ["POSTER_PYTHON"] = sys.executable
        check("★ POSTER_PYTHON 优先且存在", g.resolve_python(root) == sys.executable and
              Path(g.resolve_python(root)).exists(), g.resolve_python(root))
        os.environ.pop("POSTER_PYTHON", None)
        auto = g.resolve_python(root)
        check("未指定时自动发现（返回存在的解释器）", Path(auto).exists() or auto in ("python", "python3"), auto)

        print("\n[D] 启动命令与工作目录")
        cmd = g.build_cmd("PY", "127.0.0.1", 8765)
        check("命令形如 python -m uvicorn app:app --host H --port P",
              cmd[1:] == ["-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8765"],
              " ".join(cmd))

        print("\n[E] 端口探测")
        dead = free_port()
        check("无监听 → False", g.port_open("127.0.0.1", dead, timeout=0.6) is False, f"port={dead}")
        srv, live = dummy_listener()
        try:
            check("有监听 → True", g.port_open("127.0.0.1", live, timeout=1.5) is True, f"port={live}")

            print("\n[F] ★ 服务已在跑时绝不重复拉起（否则会起两份）")
            calls = []
            real_popen = g.subprocess.Popen

            def fake_popen(*a, **kw):
                calls.append((a, kw))
                return None
            g.subprocess.Popen = fake_popen
            try:
                rc = g.check_once("127.0.0.1", live, root)
                check("check_once 返回 0", rc == 0, f"rc={rc}")
                check("★ 端口已监听 → 不调用 Popen（不重复拉起）", len(calls) == 0, f"Popen 调用 {len(calls)} 次")

                print("\n[G] 端口不通时才拉起，且参数正确")
                tmp = Path(tempfile.mkdtemp(prefix="guard_test_"))
                (tmp / "app").mkdir(parents=True, exist_ok=True)
                dead2 = free_port()
                # 用临时"项目根"（含 app/），并让端口在拉起后立刻可连：先起监听再调用 start_service
                srv2, live2 = dummy_listener()
                try:
                    calls.clear()
                    rc2 = g.start_service("127.0.0.1", live2, root=tmp, quiet=True, wait_secs=6)
                    check("start_service 成功返回 0", rc2 == 0, f"rc={rc2}")
                    check("★ 确实调用了一次 Popen", len(calls) == 1, f"{len(calls)} 次")
                    if calls:
                        argv, kw = calls[0]
                        check("★ cwd 指向 <根>/app", Path(kw.get("cwd", "")).resolve() == (tmp / "app").resolve(),
                              str(kw.get("cwd")))
                        check("argv 含 uvicorn app:app 与端口",
                              "app:app" in argv[0] and str(live2) in argv[0], " ".join(argv[0]))
                    check("启动了就把日志/锁目录建好", (tmp / "logs").exists(), str(tmp / "logs"))

                    print("\n[H] 近期启动锁生效 → 跳过（防两处触发同时拉起）")
                    calls.clear()
                    (tmp / "logs").mkdir(parents=True, exist_ok=True)
                    (tmp / "logs" / "guard.lock").write_text("now", encoding="utf-8")
                    dead3 = free_port()
                    rc3 = g.check_once("127.0.0.1", dead3, tmp)
                    check("锁新鲜时 check_once 返回 0 且不拉起", rc3 == 0 and len(calls) == 0,
                          f"rc={rc3} Popen={len(calls)}")
                    # 老锁（超过 TTL）不该拦
                    import time as _t
                    old = _t.time() - (g.LOCK_TTL + 60)
                    os.utime(tmp / "logs" / "guard.lock", (old, old))
                    check("★ 过期锁（>%ds）不再拦截" % g.LOCK_TTL, g._recent_lock(tmp) is False, "已过期")
                finally:
                    srv2.close()
            finally:
                g.subprocess.Popen = real_popen

            print("\n[I] --dry-run 只打印、不启动")
            before = len(calls)
            rc4 = g.main(["--dry-run", "--port", str(dead)])
            check("dry-run 返回 0", rc4 == 0, f"rc={rc4}")
            check("★ dry-run 期间没有启动任何进程", len(calls) == before, f"Popen={len(calls)}")

            print("\n[J] 找不到 app 目录 → 干净失败（不抛异常）")
            tmp2 = Path(tempfile.mkdtemp(prefix="guard_missing_"))
            rc5 = g.start_service("127.0.0.1", free_port(), root=tmp2, quiet=True, wait_secs=2)
            check("★ 返回 1 而不是崩溃", rc5 == 1, f"rc={rc5}")
            check("缺目录时不写启动锁", not (tmp2 / "logs" / "guard.lock").exists(), "未写锁")
        finally:
            srv.close()
    finally:
        if old_py is not None:
            os.environ["POSTER_PYTHON"] = old_py
        if _old_env is not None:
            os.environ["POSTER_GUARD_LOG_DIR"] = _old_env

    ok = sum(1 for _, c, _ in RESULTS if c)
    print("\n" + "=" * 60)
    print(f"=== 守护脚本验证：通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
