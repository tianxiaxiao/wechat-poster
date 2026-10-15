"""
make_package.py — 把本机运行中的「润致公众号自动发布工具」打包成可迁移的 zip。

用法（在本机项目里执行）：
    python migration/make_package.py                # 打包程序本体（小，建议默认）
    python migration/make_package.py --with-profile # 额外打包微信登录态（约 288MB，可不带）

产物（都在 migration/ 目录下）：
    poster-migration.zip          程序本体（代码+页面+配置+数据+封面素材）
    wx-profile.zip                微信扫码登录态（可选；跨机器可能失效，失效则重新扫码登录）

新电脑上：解压 → 运行 install.py → 启动 start.bat
"""
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
HOME = Path.home()

# 打包进 zip 的顶层内容（migration 目录本身不打进去，其内容单独放到 zip 根目录）
INCLUDE_DIRS = ["app", "wechat", "docker"]
INCLUDE_FILES = [".dockerignore"]
# 这些迁移辅助文件必须放在 zip 根目录（解压后 install.py 才能正确识别项目根）
ROOT_LEVEL_FILES = ["install.py", "README-迁移到新电脑.md"]

# 排除规则
EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", "qrcodes", "logs"}
EXCLUDE_FILE_NAMES = {"poster-api-1.0.tar.gz", "poster-migration.zip", "wx-profile.zip"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo", ".pid"}
EXCLUDE_PREFIXES = ("diag_", "probe_", "_real_")
EXCLUDE_GLOBS = ("*.pyc", "diag_*.png", "probe_*.png", "test_articles.json")


def _skip(path: Path, name: str) -> bool:
    if name in EXCLUDE_FILE_NAMES:
        return True
    if path.is_dir() and name in EXCLUDE_DIRS:
        return True
    if path.is_file():
        if path.suffix.lower() in EXCLUDE_SUFFIXES:
            return True
        if any(name.startswith(p) for p in EXCLUDE_PREFIXES):
            return True
        for g in EXCLUDE_GLOBS:
            if path.match(g):
                return True
    return False


def add_tree(zf: zipfile.ZipFile, base: Path, arc_prefix: str, stats: dict):
    for p in sorted(base.rglob("*")):
        rel = p.relative_to(base)
        if any(part in EXCLUDE_DIRS for part in rel.parts[:-1]):
            continue
        if _skip(p, p.name):
            continue
        if p.is_dir():
            continue
        arc = f"{arc_prefix}/{rel.as_posix()}" if arc_prefix else rel.as_posix()
        zf.write(p, arc)
        stats["count"] = stats.get("count", 0) + 1
        stats["bytes"] = stats.get("bytes", 0) + p.stat().st_size


def build_program() -> Path:
    out = HERE / "poster-migration.zip"
    stats = {}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for d in INCLUDE_DIRS:
            base = ROOT / d
            if base.is_dir():
                add_tree(zf, base, d, stats)
        for f in INCLUDE_FILES:
            fp = ROOT / f
            if fp.is_file():
                zf.write(fp, f)
                stats["count"] = stats.get("count", 0) + 1
                stats["bytes"] = stats.get("bytes", 0) + fp.stat().st_size
        # 迁移辅助文件（install.py / 说明文档）放到 zip 根目录
        for f in ROOT_LEVEL_FILES:
            fp = HERE / f
            if fp.is_file():
                zf.write(fp, f)
                stats["count"] = stats.get("count", 0) + 1
                stats["bytes"] = stats.get("bytes", 0) + fp.stat().st_size
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"✅ 程序包已生成: {out}")
    print(f"   文件数 {stats.get('count', 0)}，压缩后 {size_mb:.1f} MB")
    return out


def build_profile() -> Path:
    """打包微信登录态（wx-profile）。跨机器可能失效，失效时在新电脑重新扫码登录即可。"""
    src = HOME / ".workbuddy" / "wx-profile"
    out = HERE / "wx-profile.zip"
    if not src.is_dir():
        print(f"⚠️ 未找到登录态目录 {src}，跳过")
        return None
    stats = {}
    lock_names = {"SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"}
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as zf:
        for p in src.rglob("*"):
            if p.name in lock_names:
                continue
            if any(part in ("Cache", "Code Cache", "GPUCache", "ShaderCache") for part in p.parts):
                continue
            if p.is_file():
                arc = "wx-profile/" + p.relative_to(src).as_posix()
                try:
                    zf.write(p, arc)
                    stats["count"] = stats.get("count", 0) + 1
                    stats["bytes"] = stats.get("bytes", 0) + p.stat().st_size
                except Exception:
                    pass
    size_mb = out.stat().st_size / 1024 / 1024
    print(f"✅ 登录态包已生成: {out}")
    print(f"   文件数 {stats.get('count', 0)}，压缩后 {size_mb:.1f} MB（缓存已剔除）")
    return out


def main():
    with_profile = "--with-profile" in sys.argv
    print("=" * 60)
    print("打包「润致公众号自动发布工具」迁移包")
    print("=" * 60)
    build_program()
    if with_profile:
        print()
        build_profile()
    print()
    print("下一步：把 migration/ 下的 zip 拷到新电脑，按 README-迁移到新电脑.md 操作。")


if __name__ == "__main__":
    main()
