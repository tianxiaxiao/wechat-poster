"""
SQLite ORM — 持久化：账号、主题、文章、任务、用户、操作日志
"""
import os
from sqlalchemy import Column, Integer, String, Text, DateTime, create_engine
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
    """公众号账号"""
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String(64), nullable=False)  # 例如"百秘一梳服务号"
    appid = Column(String(64), nullable=False, unique=True)
    secret_enc = Column(Text, nullable=False)  # secret（本地工具，明文存）
    author = Column(String(16), nullable=False, default="百秘一梳")  # 该号默认作者
    created_at = Column(DateTime, default=_dt.datetime.utcnow)


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
