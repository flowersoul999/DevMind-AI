"""数据库会话与引擎（数据访问基础层）。

这个文件属于「数据访问基础层」，是后端连接数据库的总入口。它做三件事：
1) 定义一个所有 ORM 模型都要继承的基类 Base（等价于「告诉 SQLAlchemy 这些类要映射到表」）；
2) 创建一个全局 engine（数据库引擎，代表和数据库的一条「高速公路」）；
3) 提供 get_db() 这个依赖函数，让每个 HTTP 请求都能拿到一个独立的数据库会话（Session），
   用完自动关闭，避免连接泄漏。

关键内容：
- Base：ORM 模型的公共基类。
- engine：数据库连接引擎，全局一个。
- SessionLocal：会话工厂，每次调用产出一个新会话。
- get_db()：FastAPI 依赖，给接口函数提供 db 会话。

和其他模块的配合：
- db/models.py 里所有模型都写 `from app.db.session import Base` 并继承它；
- main.py / 各路由通过 `Depends(get_db)` 拿到 db 来增删改查。
可以把它类比成「公司的总闸 + 每个员工领的一张临时门禁卡」：engine 是总闸，
SessionLocal 是发卡机，get_db 保证每张卡用完就回收。."""

# 从标准库 collections.abc 引入 Generator：用来标注 get_db 是个「生成器函数」（配合 yield 做上下文管理）
from collections.abc import Generator

# 引入 SQLAlchemy 的 create_engine：用来创建数据库引擎
from sqlalchemy import create_engine
# 引入 ORM 相关：DeclarativeBase（声明式基类）、Session（会话）、sessionmaker（会话工厂）
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

# 引入全局配置对象，拿到数据库连接串
from app.core.config import settings


# 定义 ORM 模型的公共基类 Base，继承 SQLAlchemy 的 DeclarativeBase
# 为什么：所有模型类继承它，SQLAlchemy 才知道「这个类对应一张表」，这是 2.0 风格的写法
class Base(DeclarativeBase):
    """Base."""
    # 空类体：它本身不定义字段，只是个标记/基类，具体字段由各模型自己写
    pass


# 创建数据库引擎：用配置里的 database_url 连库；pool_pre_ping=True 让连接池每次取连接前先 ping 一下
# 为什么：pool_pre_ping 相当于「拨号前先听一下有没有忙音」，能自动丢弃已断开的死连接，避免拿到坏连接报错
engine = create_engine(settings.database_url, pool_pre_ping=True)
# 创建会话工厂 SessionLocal：bind=engine 表示它产出的会话都走上面那个引擎；
# autoflush=False 不自动刷盘，autocommit=False 不自动提交（要手动 commit，更安全可控）
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


# 定义一个生成器函数 get_db，作为 FastAPI 的「依赖」：每次请求来时给一个 db，结束后关掉
def get_db() -> Generator[Session, None, None]:
    # 用会话工厂新建一个数据库会话 db
    db = SessionLocal()
    # try 里把 db 交给接口函数使用（yield 会暂停在这里，把 db 返回给调用方）
    try:
        # 把会话交出去，接口函数执行期间用的就是这一个 db
        yield db
    # 不管前面成功还是抛异常， finally 都会执行
    finally:
        # 用完了：关闭会话，把连接还给连接池，防止连接越积越多把数据库拖垮
        db.close()
