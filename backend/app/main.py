"""应用入口（Web 服务主程序 / 应用工厂）。

这个文件属于「Web 服务主程序」，是整个后端进程的启动入口。它做几件核心事：
1) 用「应用工厂」create_app() 组装出一个 FastAPI 应用（注册中间件、挂载所有路由、定义健康检查）；
2) 在启动时自动建表/加字段（课程项目偷懒用 create_all，省去迁移工具）；
3) 启动后台定时任务（自动同步 GitHub 数据），并在关闭时优雅停止。
最底部 `app = create_app()` 真正把应用造出来，给 uvicorn 当入口。

关键内容：
- _cors_origins() / _cors_origin_regex()：算出允许跨域的来源。
- create_app()：应用工厂，返回配置好的 FastAPI 实例。
- 内部 health()：探活接口。
- 内部 create_demo_tables()（startup 事件）：启动时建表 + 补字段 + 初始化数据 + 起后台同步。
- 内部 stop_background_tasks()（shutdown 事件）：停掉后台任务。

和其他模块的配合：
- 引入 app.api.routes 下的一堆路由模块（repos/issues/ci/...）并挂载；
- 用 app.core.config.settings 读配置、app.db.models.Base 和 app.db.session.engine 操作数据库；
- 调 app.services.scheduler 的定时循环。
可以把它类比成「做一杯奶茶的流水线」：create_app 就是流水线总开关，
中间件是封口膜，路由是一排配料口（珍珠/奶茶/布丁），startup 是开机预热并铺好原料，shutdown 是关机收摊。."""

# 引入 asyncio：用来创建/取消后台异步任务（自动同步循环）
import asyncio

# 从 fastapi 引入 FastAPI 框架主类
from fastapi import FastAPI
# 引入 CORS 中间件：解决「前端别的域名访问后端」的跨域问题
from fastapi.middleware.cors import CORSMiddleware
# 引入 JSONResponse：用来手动构造 JSON 格式的 HTTP 响应
from fastapi.responses import JSONResponse
# 从 sqlalchemy 引入 inspect（查看库里已有哪些表/字段）和 text（写原生 SQL）
from sqlalchemy import inspect, text

# 一次性引入所有路由模块（每个模块里有一个 router 对象，代表一组接口）
from app.api.routes import (
    # 动作草稿相关接口
    action_drafts,
    # 聊天接口
    chat,
    # CI（持续集成）接口
    ci,
    # 代码图谱接口
    code_graph,
    # 会话（conversation）接口
    conversations,
    # 评测（evaluation）接口
    evaluations,
    # 反馈接口
    feedback,
    # Issue 接口
    issues,
    # 知识库接口
    knowledge,
    # 项目索引接口
    project_index,
    # Pull Request 接口
    pull_requests,
    # RAG 检索接口
    rag,
    # 仓库接口
    repos,
    # 报告接口
    reports,
    # 搜索接口
    search,
    # 技能接口
    skills,
    # GitHub Webhook 回调接口
    webhooks,
    # 工作空间接口
    workspaces,
)
# 引入全局配置对象 settings
from app.core.config import settings
# 引入 ORM 公共基类 Base（建表要用到它的 metadata）
from app.db.models import Base
# 引入数据库引擎 engine（建表/执行 SQL 要用）
from app.db.session import engine
# 引入后台定时同步循环函数
from app.services.scheduler import periodic_repo_sync_loop
# 引入 Milvus 不可用时抛出的自定义异常（用来做专属错误处理）
from app.services.rag.vector_store import MilvusUnavailableError


# 内部函数：把配置里的 cors_origins 字符串按逗号拆成列表，并去掉首尾空格和空项
def _cors_origins() -> list[str]:
    # 用 split(",") 切开字符串，对每个 origin 去掉空格，再用 if 过滤掉空字符串，最后组成列表返回
    return [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]


# 内部函数：返回允许跨域的正则表达式；生产环境返回 None（只认精确名单），非生产返回 localhost 通配
def _cors_origin_regex() -> str | None:
    # 判断当前环境是不是 production（先把 app_env 转小写再比，避免大小写问题）
    if settings.app_env.lower() == "production":
        # 生产环境：不开放正则通配，只允许上面精确列出的来源，更安全
        return None
    # 非生产（开发/演示）：允许 localhost / 127.0.0.1 / IPv6 回环地址 + 任意端口，方便本地联调
    return r"^https?://(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$"


# 应用工厂：组装并返回配置好的 FastAPI 应用实例
# 为什么：用工厂函数而不是直接写全局 app，好处是可以「按需要造出多个独立实例」，
# 比如测试时造一个不挂真实后台任务的 app，互不干扰；就像流水线总开关，按一下产出一杯完整奶茶
def create_app() -> FastAPI:
    # 创建一个 FastAPI 实例，title 用配置里的应用名，版本写死 0.1.0，会显示在接口文档上
    app = FastAPI(title=settings.app_name, version="0.1.0")

    # 注册一个异常处理器：当代码里抛出 MilvusUnavailableError 时，统一返回 503 降级响应
    @app.exception_handler(MilvusUnavailableError)
    # 处理器函数：收到任意 request（这里用 _ 占位不用）和具体异常 exc
    async def milvus_unavailable_handler(_request, exc: MilvusUnavailableError) -> JSONResponse:
        # 返回一个 JSON 响应：状态码 503（服务不可用），内容说明哪个服务降级了、原因是什么
        return JSONResponse(
            # HTTP 状态码 503
            status_code=503,
            # 响应体：detail 是异常信息，service 标明是 milvus，status 标明降级
            content={"detail": str(exc), "service": "milvus", "status": "degraded"},
        )
    # 给应用加上 CORS 中间件，允许浏览器跨域访问
    app.add_middleware(
        # 使用官方 CORS 中间件
        CORSMiddleware,
        # 精确允许的跨域来源列表（来自 _cors_origins()）
        allow_origins=_cors_origins(),
        # 正则匹配的跨域来源（开发环境放行 localhost）
        allow_origin_regex=_cors_origin_regex(),
        # 是否允许携带凭证（cookie/授权头）；为 True 时上面来源不能是 "*"
        allow_credentials=True,
        # 允许所有 HTTP 方法（GET/POST/...）
        allow_methods=["*"],
        # 允许所有请求头
        allow_headers=["*"],
    )

    # 把各个路由模块挂到应用上，并指定 URL 前缀和文档标签
    # 仓库相关接口，前缀 /api/repos
    app.include_router(repos.router, prefix="/api/repos", tags=["repos"])
    # 会话接口，也挂在 /api/repos 下（同一个仓库上下文）
    app.include_router(conversations.router, prefix="/api/repos", tags=["conversations"])
    # 工作空间接口，前缀 /api/workspaces
    app.include_router(workspaces.router, prefix="/api/workspaces", tags=["workspaces"])
    # Issue 接口，前缀 /api
    app.include_router(issues.router, prefix="/api", tags=["issues"])
    # Pull Request 接口，前缀 /api
    app.include_router(pull_requests.router, prefix="/api", tags=["pull_requests"])
    # CI 接口，前缀 /api
    app.include_router(ci.router, prefix="/api", tags=["ci"])
    # 聊天接口，前缀 /api/chat
    app.include_router(chat.router, prefix="/api/chat", tags=["chat"])
    # 聊天反馈接口，前缀 /api/chat/feedback
    app.include_router(feedback.router, prefix="/api/chat/feedback", tags=["chat_feedback"])
    # 动作草稿接口，前缀 /api/action-drafts
    app.include_router(action_drafts.router, prefix="/api/action-drafts", tags=["action_drafts"])
    # 报告接口，前缀 /api/reports
    app.include_router(reports.router, prefix="/api/reports", tags=["reports"])
    # 搜索接口，前缀 /api/search
    app.include_router(search.router, prefix="/api/search", tags=["search"])
    # 技能接口，前缀 /api/skills
    app.include_router(skills.router, prefix="/api/skills", tags=["skills"])
    # 知识库接口，前缀 /api/repos
    app.include_router(knowledge.router, prefix="/api/repos", tags=["knowledge"])
    # 项目索引接口，前缀 /api/repos
    app.include_router(project_index.router, prefix="/api/repos", tags=["project_index"])
    # RAG 接口，前缀 /api/rag
    app.include_router(rag.router, prefix="/api/rag", tags=["rag"])
    # 代码图谱接口，前缀 /api
    app.include_router(code_graph.router, prefix="/api", tags=["code_graph"])
    # 评测接口，前缀 /api/evals
    app.include_router(evaluations.router, prefix="/api/evals", tags=["evaluations"])
    # Webhook 接口，前缀 /api/webhooks
    app.include_router(webhooks.router, prefix="/api/webhooks", tags=["webhooks"])
    # 定义一个最简单的健康检查接口：GET /health
    @app.get("/health")
    # 异步函数 health，返回一个字典（FastAPI 会自动转成 JSON）
    async def health() -> dict[str, str]:
        # 返回状态 ok 和应用名，给运维/前端探活用
        return {"status": "ok", "app": settings.app_name}

    # 注册「应用启动」事件：服务起来时自动执行下面的初始化
    @app.on_event("startup")
    # 异步函数：启动时建表 + 补字段 + 初始化数据 + 起后台同步
    async def create_demo_tables() -> None:
        # 课程项目默认自动建表；生产风格流程会在后续加入 Alembic 迁移。
        # 根据 Base 里所有模型的定义，在数据库里把缺失的表一次性建出来；bind=engine 指定连哪个库
        Base.metadata.create_all(bind=engine)
        # 创建一个检查器 inspector，用来查看库里已经存在哪些表、哪些字段
        inspector = inspect(engine)
        # 如果用户表存在，检查是否需要补 role 字段
        if "users" in inspector.get_table_names():
            # 取出 users 表现有的字段名，放进一个集合方便查「有没有某列」
            columns = {column["name"] for column in inspector.get_columns("users")}
            # 如果没有 role 列，就补上（默认 owner）
            if "role" not in columns:
                # 用 engine.begin() 开一个会自动提交的事务连接
                with engine.begin() as connection:
                    # 执行原生 SQL：给 users 表加一列 role，字符串类型，默认 'owner'
                    connection.execute(text("ALTER TABLE users ADD COLUMN role VARCHAR(80) DEFAULT 'owner'"))
        # 如果 repositories 表存在，检查并补齐一系列仓库相关字段
        if "repositories" in inspector.get_table_names():
            # 取出现有字段名集合
            columns = {column["name"] for column in inspector.get_columns("repositories")}
            # 开一个自动提交的事务连接
            with engine.begin() as connection:
                # 没有 provider 列就补上，默认 'github'
                if "provider" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN provider VARCHAR(80) DEFAULT 'github'"))
                # 没有 api_base_url 列就补上（存自建 Git 平台的地址）
                if "api_base_url" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN api_base_url TEXT"))
                # 没有 clone_url 列就补上（克隆地址）
                if "clone_url" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN clone_url TEXT"))
                # 没有 local_path 列就补上（本地克隆路径）
                if "local_path" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN local_path TEXT"))
                # 没有 checkout_mode 列就补上，默认 'managed'
                if "checkout_mode" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN checkout_mode VARCHAR(80) DEFAULT 'managed'"))
                # 没有 github_token_encrypted 列就补上（加密后的 Token）
                if "github_token_encrypted" not in columns:
                    connection.execute(text("ALTER TABLE repositories ADD COLUMN github_token_encrypted TEXT"))
        # 如果 workflow_runs 表存在，检查是否需要补 jobs 字段
        if "workflow_runs" in inspector.get_table_names():
            # 取出现有字段名集合
            columns = {column["name"] for column in inspector.get_columns("workflow_runs")}
            # 没有 jobs 列就补上，类型 JSON（存 CI 各 job 详情）
            if "jobs" not in columns:
                # 开自动提交事务
                with engine.begin() as connection:
                    # 执行原生 SQL 加 jobs 列，JSON 类型
                    connection.execute(text("ALTER TABLE workflow_runs ADD COLUMN jobs JSON"))
        # 对 code_symbols、code_relations 两张代码图谱表，统一补 pr_id / pr_number 字段
        for table_name in ["code_symbols", "code_relations"]:
            # 表存在才处理
            if table_name in inspector.get_table_names():
                # 取出现有字段名集合
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                # 开自动提交事务
                with engine.begin() as connection:
                    # 没有 pr_id 列就补上（UUID，关联到某个 PR）
                    if "pr_id" not in columns:
                        # f-string 把表名拼进 SQL（表名是写死的白名单，安全）
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN pr_id UUID"))
                    # 没有 pr_number 列就补上（PR 编号，整数）
                    if "pr_number" not in columns:
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN pr_number INTEGER"))
        # 对 chat_messages / chat_sessions / agent_runs / evidence_items 四张表，统一补 conversation_id 字段
        for table_name in ["chat_messages", "chat_sessions", "agent_runs", "evidence_items"]:
            # 表存在才处理
            if table_name in inspector.get_table_names():
                # 取出现有字段名集合
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                # 没有 conversation_id 列就补上（关联到会话）
                if "conversation_id" not in columns:
                    # 开自动提交事务
                    with engine.begin() as connection:
                        # 用 f-string 拼表名，加 UUID 类型的 conversation_id 列
                        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN conversation_id UUID"))
        # 对 thread_memories / conversation_memories 两张记忆表，补一批 JSON 字段
        for table_name in ["thread_memories", "conversation_memories"]:
            # 表存在才处理
            if table_name in inspector.get_table_names():
                # 取出现有字段名集合
                columns = {column["name"] for column in inspector.get_columns(table_name)}
                # 逐个检查要补的字段名
                for column_name in ["facts", "tasks", "user_preferences", "repo_context", "citations"]:
                    # 该字段不存在才加
                    if column_name not in columns:
                        # 开自动提交事务
                        with engine.begin() as connection:
                            # f-string 拼表名和列名，加 JSON 类型列
                            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} JSON"))
        # 如果 documents 表存在，检查是否还留着旧的 embedding 列
        if "documents" in inspector.get_table_names():
            # 取出现有字段名集合
            document_columns = {column["name"] for column in inspector.get_columns("documents")}
            # 如果还有 embedding 列（旧设计把向量存表里，现改存 Milvus）
            if "embedding" in document_columns:
                # 开自动提交事务
                with engine.begin() as connection:
                    # 把 embedding 列删掉，向量以后只放外部向量库
                    connection.execute(text("ALTER TABLE documents DROP COLUMN embedding"))
        # 延迟引入：会话工厂和几个服务函数（放在函数内引入，避免模块加载顺序/循环依赖问题）
        from app.db.session import SessionLocal
        # 引入「给所有仓库补默认会话」的服务函数
        from app.services.conversations import backfill_all_default_conversations
        # 引入「清理旧版代码文档」的服务函数
        from app.services.code_analysis import purge_legacy_code_documents
        # 引入 Milvus 相关函数：删文档、查运行状态
        from app.services.rag.vector_store import delete_milvus_documents, milvus_runtime_status

        # 用会话工厂新建一个数据库会话 db
        db = SessionLocal()
        # try 里做初始化数据操作
        try:
            # 给每个仓库补上默认会话（保证前端一定有会话可用）
            backfill_all_default_conversations(db)
            # 清理旧版代码文档；如果清了东西就提交
            if purge_legacy_code_documents(db):
                # 提交事务，让删除生效
                db.commit()
        # 不管成功失败，finally 都会执行
        finally:
            # 关闭会话，归还连接
            db.close()
        # 把 Milvus 实时运行状态记到 app.state 上（供接口/前端读取向量库是否可用）
        app.state.rag_vector_status = milvus_runtime_status()
        # 删除 Milvus 里 source_type 为 code_file 的文档（和上面 purge 配合，清掉旧代码向量）
        delete_milvus_documents(source_types=["code_file"], strict=False)
        # 如果配置允许自动同步
        if settings.auto_sync_enabled:
            # 在应用状态上记一个后台任务：启动定时同步 GitHub 数据的循环
            # 为什么：asyncio.create_task 把它变成「后台小工」，主线程继续接请求，小工按间隔去拉数据
            app.state.repo_sync_task = asyncio.create_task(periodic_repo_sync_loop())

    # 注册「应用关闭」事件：服务停时执行清理
    @app.on_event("shutdown")
    # 异步函数：停止后台任务
    async def stop_background_tasks() -> None:
        # 从应用状态里取出后台同步任务（没有就取 None）
        task = getattr(app.state, "repo_sync_task", None)
        # 如果任务存在
        if task:
            # 取消这个异步任务，让它停下来（优雅停机，不残留野线程）
            task.cancel()

    # 把组装好的 FastAPI 应用实例返回出去
    return app


# 在模块加载时真正调用工厂，造出全局 app 实例；uvicorn 运行的就是这个 app
app = create_app()
