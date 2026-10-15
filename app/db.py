"""
SQLite ORM — 持久化：账号、主题、文章、任务、用户、操作日志
"""
import os
from sqlalchemy import Column, Integer, String, Text, DateTime, create_engine, text
from sqlalchemy.orm import sessionmaker, declarative_base, Session
import datetime as _dt
from pathlib import Path

# 数据库位置可用环境变量 POSTER_DB 覆盖（NAS 上挂数据卷 / 自动化测试隔离用）
DB_PATH = Path(os.getenv("POSTER_DB") or (Path(__file__).parent / "data" / "poster.db"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False}, echo=False)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class Account(Base):
    """公众号账号（owner_id 为归属用户；NULL = 系统共享，所有用户可见/可发）"""
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)  # 例如"百秘一梳服务号"
    appid = Column(String(64), nullable=False, unique=True)
    secret_enc = Column(Text, nullable=False)  # secret（本地工具，明文存）
    author = Column(String(16), nullable=False, default="百秘一梳")  # 该号默认作者
    owner_id = Column(Integer, index=True)      # 归属用户 User.id；NULL = 系统共享
    created_at = Column(DateTime, default=_dt.datetime.utcnow)


class UserSetting(Base):
    """每用户私有配置（2026-10-15 多用户多租户改造）：自己的 LLM 模型/Key + 自己的定时计划"""
    __tablename__ = "user_settings"
    user_id = Column(Integer, primary_key=True)              # 关联 User.id
    llm_provider = Column(String(32), default="deepseek")    # 该用户默认 provider
    llm_base_url = Column(String(255), default="")           # 该用户默认 base_url
    llm_model = Column(String(255), default="")              # 该用户默认 model
    llm_api_key = Column(Text, default="")                   # 该用户自己的 API Key（各人消耗各人的）
    default_account_id = Column(Integer, default=0)          # 默认发布到的公众号
    schedule_enabled = Column(Integer, default=0)            # 该用户是否开启每日定时
    schedule_time = Column(String(8), default="10:00")       # 定时时刻 HH:MM
    schedule_account_ids = Column(Text, default="")          # JSON list[int]：勾选要定时发布的自有公众号（空=用 default_account_id）
    schedule_product_id = Column(Integer, default=0)         # 定时使用的产品
    schedule_last_run_date = Column(String(16), default="")  # 当天已触发日期，防重复


class Product(Base):
    """产品/品牌库：写软文时选择对应产品介绍"""
    __tablename__ = "products"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)  # 产品名，如"百秘一梳"、"xxx护肤品"
    intro = Column(Text, nullable=False, default="")  # 产品介绍/卖点/品牌故事
    style = Column(String(255), nullable=False, default="")  # 可选的风格色板提示
    created_at = Column(DateTime, default=_dt.datetime.utcnow)


class Topic(Base):
    """主题库：常用软文主题词（可多选，每次批量生成）"""
    __tablename__ = "topics"
    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False)  # 主题词，如"偏头痛"、"失眠"、"脱发"
    desc = Column(String(255), nullable=False, default="")  # 可选备注/默认角度提示
    # 归属产品：>0 只在该产品下出现；0 = 通用（所有产品都能看到）。
    # 用途：把「保健功效类主题」绑到木梳，就不会出现在美食产品下（2026-10-16 新增）。
    product_id = Column(Integer, default=0, index=True)
    created_at = Column(DateTime, default=_dt.datetime.utcnow)


class Article(Base):
    """文章（生成/发布状态）"""
    __tablename__ = "articles"
    id = Column(Integer, primary_key=True)
    job_id = Column(String(64), index=True)  # 关联一次 run
    seq = Column(Integer)                    # 批次内序号
    topic = Column(String(128))
    title = Column(String(255))
    body_md = Column(Text)
    cover_path = Column(String(512))
    status = Column(String(32))  # pending/generated/publishing/published/failed
    publish_id = Column(String(64))  # 微信 article_id
    account_id = Column(Integer, default=0)  # 发布到的公众号（多租户去重用）
    theme = Column(String(64), default="")   # 卡片排版主题名（如「字节范」「阿里橙」）；扫码发布为空
    error = Column(Text)
    created_at = Column(DateTime, default=_dt.datetime.utcnow)
    updated_at = Column(DateTime, default=_dt.datetime.utcnow, onupdate=_dt.datetime.utcnow)


class Setting(Base):
    """通用 key/value 设置（定时任务配置等持久化用）"""
    __tablename__ = "settings"
    key = Column(String(64), primary_key=True)
    value = Column(Text, nullable=False, default="")
    updated_at = Column(DateTime, default=_dt.datetime.utcnow, onupdate=_dt.datetime.utcnow)


class User(Base):
    """登录用户（2026-10-14 新增：NAS + 公网访问需要鉴权）
    角色：admin 管理员 / editor 编辑（能生成发布）/ viewer 只读"""
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    name = Column(String(64), nullable=False, default="")
    role = Column(String(16), nullable=False, default="editor")   # admin/editor/viewer
    password_hash = Column(Text, nullable=False)                  # pbkdf2_sha256$...
    is_active = Column(Integer, nullable=False, default=1)        # 1 启用 / 0 停用
    must_change_pw = Column(Integer, nullable=False, default=0)   # 1 = 强制改密（初始密码）
    last_login_at = Column(DateTime)
    created_at = Column(DateTime, default=_dt.datetime.utcnow)


class AuditLog(Base):
    """操作日志（审计）：发布属于对外动作，公网部署后必须留痕"""
    __tablename__ = "audit_logs"
    id = Column(Integer, primary_key=True)
    ts = Column(DateTime, default=_dt.datetime.utcnow, index=True)
    user_id = Column(Integer)
    username = Column(String(64), index=True)
    role = Column(String(16))
    action = Column(String(64), index=True)     # auth.login / user.create / job.run ...
    detail = Column(Text, default="")
    result = Column(String(16), default="ok")   # ok / fail
    level = Column(String(16), default="info")  # info / warn / danger
    ip = Column(String(64))
    ua = Column(String(300))
    extra = Column(String(500), default="")


def get_setting(key: str, default: str = "") -> str:
    s = SessionLocal()
    try:
        r = s.get(Setting, key)
        return r.value if r else default
    finally:
        s.close()


def set_setting(key: str, value: str):
    s = SessionLocal()
    try:
        r = s.get(Setting, key)
        if r:
            r.value = value
        else:
            s.add(Setting(key=key, value=value))
        s.commit()
    finally:
        s.close()


def init_db():
    Base.metadata.create_all(engine)
    _migrate_schema()


def _migrate_schema():
    """兼容旧库：给已存在的表补新增列（create_all 不会给已存在表加列）：
    · accounts.owner_id  —— 多租户归属（NULL=系统共享）
    · articles.account_id —— 多租户去重
    · articles.theme      —— 卡片排版主题名（通知里显示「（字节范）」等）"""
    try:
        with engine.connect() as c:
            cols = [r[1] for r in c.execute(text("PRAGMA table_info(accounts)")).fetchall()]
            if "owner_id" not in cols:
                c.execute(text("ALTER TABLE accounts ADD COLUMN owner_id INTEGER"))
                c.commit()
            cols2 = [r[1] for r in c.execute(text("PRAGMA table_info(articles)")).fetchall()]
            if "account_id" not in cols2:
                c.execute(text("ALTER TABLE articles ADD COLUMN account_id INTEGER DEFAULT 0"))
                c.commit()
            if "theme" not in cols2:
                c.execute(text("ALTER TABLE articles ADD COLUMN theme VARCHAR(64) DEFAULT ''"))
                c.commit()
            cols3 = [r[1] for r in c.execute(text("PRAGMA table_info(topics)")).fetchall()]
            if cols3 and "product_id" not in cols3:
                # 旧库已有主题一律视为「通用(0)」→ 所有产品仍可见，不会突然消失
                c.execute(text("ALTER TABLE topics ADD COLUMN product_id INTEGER DEFAULT 0"))
                c.commit()
    except Exception as e:  # noqa: BLE001
        print(f"[migrate] 列迁移失败（可忽略）: {e}")


# ---- 每用户配置便捷函数 ----
def get_user_setting(user_id: int, db: Session = None):
    s = db or SessionLocal()
    try:
        return s.get(UserSetting, user_id)
    finally:
        if db is None:
            s.close()


def upsert_user_setting(user_id: int, db: Session = None, **fields) -> UserSetting:
    s = db or SessionLocal()
    try:
        r = s.get(UserSetting, user_id)
        if not r:
            r = UserSetting(user_id=user_id)
            s.add(r)
        for k, v in fields.items():
            if hasattr(r, k):
                setattr(r, k, v)
        s.commit()
        s.refresh(r)
        return r
    finally:
        if db is None:
            s.close()


def list_accounts_for_user(user_id: int, is_admin: bool, db: Session = None):
    """列出某用户可见的公众号：管理员看全部；普通用户看自己的 + 系统共享(NULL)。"""
    s = db or SessionLocal()
    try:
        q = s.query(Account)
        if not is_admin:
            q = q.filter((Account.owner_id == user_id) | (Account.owner_id.is_(None)))
        return q.order_by(Account.id).all()
    finally:
        if db is None:
            s.close()


# ---- Product / Account CRUD 便捷函数 ----
def list_products(db: Session = None) -> list:
    s = db or SessionLocal()
    try:
        return s.query(Product).order_by(Product.id).all()
    finally:
        if db is None: s.close()


def add_product(db: Session, name: str, intro: str, style: str = "") -> Product:
    p = Product(name=name, intro=intro, style=style)
    db.add(p); db.commit(); db.refresh(p)
    return p


def list_accounts(db: Session = None) -> list:
    s = db or SessionLocal()
    try:
        return s.query(Account).order_by(Account.id).all()
    finally:
        if db is None: s.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ---- Topic 主题库 CRUD ----
def list_topics(db: Session = None) -> list:
    s = db or SessionLocal()
    try:
        return s.query(Topic).order_by(Topic.id).all()
    finally:
        if db is None: s.close()


def add_topic(db: Session, name: str, desc: str = "") -> Topic:
    t = Topic(name=name, desc=desc)
    db.add(t); db.commit(); db.refresh(t)
    return t


def delete_topic(db: Session, topic_id: int) -> bool:
    t = db.query(Topic).filter(Topic.id == topic_id).first()
    if not t:
        return False
    db.delete(t); db.commit()
    return True
