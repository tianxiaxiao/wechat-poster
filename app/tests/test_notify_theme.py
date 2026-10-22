"""
test_notify_theme.py — 「通知里带卡片排版主题」验证
====================================================
背景：每日定时汇总的通知里有「（字节范）（阿里橙）…」，但**手动发布**的完成通知没有。
原因：手动发布走 notify.send_job_summary()，它自己拼行、没读 theme 字段；而 _run_job
渲染卡片时拿到了主题名却只写进日志、没存进 job/DB。

本脚本断言（不真发通知，打桩捕获文本）：
  · send_job_summary 的行里带「（主题）」；无主题（扫码发布）则不加后缀
  · build_summary_lines（每日汇总用的那条路径）主题仍正常
  · Article 模型有 theme 列
  · 【关键】旧库（articles 无 theme 列）跑 init_db() 后自动补上该列（NAS 旧库可平滑升级）
运行：python tests/test_notify_theme.py
"""
import subprocess
import sys
import tempfile
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(APP_DIR))

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (f"   [{detail}]" if detail else ""))


def main():
    import notify

    print("\n[1] send_job_summary（手动发布通知）显示主题")
    captured = {}
    orig = notify.send_markdown_chunks
    notify.send_markdown_chunks = lambda head, lines, limit_bytes=3600: (
        captured.update(head=head, lines=list(lines)) or [])
    try:
        job = {
            "source_label": "手动",
            "articles": [
                {"seq": 0, "title": "标题一", "status": "published",
                 "url": "https://mp.weixin.qq.com/s/abc", "theme": "字节范"},
                {"seq": 1, "title": "标题二", "status": "published", "url": "", "theme": "阿里橙"},
                {"seq": 2, "title": "标题三", "status": "published", "url": "", "theme": ""},      # 扫码：无主题
                {"seq": 3, "title": "标题四", "status": "published", "url": ""},                  # 字段缺失：无主题
                {"seq": 4, "title": "标题五", "status": "failed", "url": "", "theme": "笔记本"},
            ],
        }
        notify.send_job_summary(job)
    finally:
        notify.send_markdown_chunks = orig

    lines = captured.get("lines", [])
    txt = "\n".join(lines)
    check("手动通知含「（字节范）」", "（字节范）" in txt, txt[:120].replace("\n", " | "))
    check("手动通知含「（阿里橙）」", "（阿里橙）" in txt)
    check("失败篇也带主题「（笔记本）」", "（笔记本）" in txt)
    check("扫码篇（无主题）不加后缀", "标题三（" not in txt and "- ✅ 标题三" in txt)
    check("字段缺失也不报错、不加后缀", "标题四（" not in txt and "- ✅ 标题四" in txt)
    check("带链接的标题格式正确", "- ✅ [标题一](https://mp.weixin.qq.com/s/abc)（字节范）" in txt,
          next((l for l in lines if "标题一" in l), ""))
    check("head 为「手动补跑完成」口径", "手动补跑完成" in captured.get("head", ""),
          captured.get("head", ""))

    print("\n[2] build_summary_lines（每日汇总路径）主题不受影响")
    rows = notify.build_summary_lines([
        {"title": "甲", "url": "https://mp.weixin.qq.com/s/x", "theme": "字节范", "status": "published"},
        {"title": "乙", "url": "", "theme": "", "status": "published"},
    ])
    check("汇总行含「（字节范）」", "（字节范）" in rows[0], rows[0])
    check("汇总行无主题时不留空括号", "（）" not in rows[1], rows[1])
    check("两处格式一致（同为 `- ✅ [标题](链接)（主题）`）",
          rows[0].startswith("- ✅ [甲](https://mp.weixin.qq.com/s/x)（字节范）"), rows[0])

    print("\n[3] Article 模型已带 theme 列")
    import db
    cols = [c.name for c in db.Article.__table__.columns]
    check("Article.theme 存在", "theme" in cols, str(cols))

    print("\n[4] 【关键】旧库自动迁移：articles 无 theme 列 → init_db() 后自动补上")
    tmp = Path(tempfile.mkdtemp(prefix="poster_theme_"))
    db_path = tmp / "old.db"
    code = (
        "import os, sqlite3, sys\n"
        "db_path, app_dir = sys.argv[1], sys.argv[2]\n"
        "con = sqlite3.connect(db_path)\n"
        "con.execute('create table articles (id integer primary key, job_id varchar(64), seq integer,'\n"
        "            ' topic varchar(128), title varchar(255), body_md text, cover_path varchar(512),'\n"
        "            ' status varchar(32), publish_id varchar(64), error text, created_at datetime,'\n"
        "            ' updated_at datetime)')\n"          # 旧结构：没有 theme 列
        "con.commit(); con.close()\n"
        "os.environ['POSTER_DB'] = db_path\n"
        "sys.path.insert(0, app_dir)\n"
        "import db\n"
        "db.init_db()\n"
        "con = sqlite3.connect(db_path)\n"
        "cols = [r[1] for r in con.execute('pragma table_info(articles)')]\n"
        "print('HAS_THEME' if 'theme' in cols else 'NO_THEME:' + ','.join(cols))\n"
    )
    r = subprocess.run([sys.executable, "-c", code, str(db_path), str(APP_DIR)],
                       capture_output=True, text=True, timeout=120)
    out = (r.stdout or "").strip().splitlines()[-1] if r.stdout.strip() else (r.stderr or "")[-200:]
    check("旧库迁移后 articles 出现 theme 列", out == "HAS_THEME", out)

    print("\n" + "=" * 56)
    ok = sum(1 for _, c, _ in RESULTS if c)
    print(f"=== 通过 {ok} / 共 {len(RESULTS)}；失败 {len(RESULTS) - ok} ===")
    return 0 if ok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
