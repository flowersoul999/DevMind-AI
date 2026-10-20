"""DevMind AI module."""

# skills.py —— Skill（智能体技能）与 MCP 运行时状态的路由层
#
# 业务域：平台「技能市场」。Skill 是预先写好的一段可复用提示词/工具集合，
# 让 ChatAgent 在对话里能调用特定能力（比如「issue 分析」「PR 审查」）。
# 本文件把前端的「列出技能」「查看某个技能详情」「检查 MCP 服务是否在线」请求，
# 转交给 app.services.skills（技能注册表）和 app.services.mcp_client（MCP 客户端）处理。
#
# 暴露端点：
#   GET /api/skills          —— 列出所有已注册技能
#   GET /api/skills/mcp-status —— 探测 MCP 服务器是否在线、返回支持的工具列表
#   GET /api/skills/{skill_name} —— 查看单个技能详情（含完整使用说明）
#
# 依赖 service：app.services.skills.get_skill_registry、app.services.mcp_client.StdioMcpClient
from fastapi import APIRouter, HTTPException  # 引入 FastAPI 的路由器和 HTTP 异常类

# 引入本文件用到的响应/数据模型（Skill 列表、单个 Skill、MCP 状态等）
from app.schemas.skills import McpRuntimeResponse, McpToolResponse, SkillListResponse, SkillManifestResponse, SkillResponse
# 引入 MCP 客户端：用来连接外部「模型上下文协议」服务器，并捕获它的错误
from app.services.mcp_client import McpClientError, StdioMcpClient
# 引入技能注册表：一个全局单例，保存所有已加载的 Skill
from app.services.skills import get_skill_registry

# 创建一个 APIRouter 实例。可把 router 类比成「餐厅菜单」，下面每个 @router.xxx 就是「一道菜」（一个 HTTP 端点）
router = APIRouter()


def _skill_response(skill, include_instructions: bool = False) -> SkillResponse:
    # 把内部 skill 对象转换成对外返回的 SkillResponse；include_instructions 决定要不要带「使用说明」
    return SkillResponse(
        # manifest：把 skill 自带的清单（名称、描述、入参）转成对外模型
        manifest=SkillManifestResponse.model_validate(skill.manifest.model_dump()),
        path=skill.path,  # path：这个 skill 在磁盘上的来源路径
        # 是否返回给前端完整 instructions 文本（详情页才需要，列表页不需要）
        instructions=skill.instructions if include_instructions else None,
    )


@router.get("", response_model=SkillListResponse)  # GET 请求，路径为空字符串（相对 router 前缀，完整路径形如 /api/skills）；返回 SkillListResponse
async def list_skills() -> SkillListResponse:
    # 处理「列出所有技能」的请求，是一个异步函数（async 让它能非阻塞地处理并发请求）
    registry = get_skill_registry()  # 拿到技能注册表单例
    # 把注册表里每个 skill 转成对外模型，再包成列表返回
    return SkillListResponse(skills=[_skill_response(skill) for skill in registry.list_skills()])


@router.get("/mcp-status", response_model=McpRuntimeResponse)  # GET /api/skills/mcp-status，返回 MCP 运行时状态
async def get_mcp_status() -> McpRuntimeResponse:
    # 探测外部 MCP 服务器是否「活着」，并把它支持的工具列出来
    try:
        # 尝试用 stdio（标准输入输出）方式启动并询问 MCP 服务器
        inspection = await StdioMcpClient().inspect_server()
    except (McpClientError, OSError) as exc:
        # 如果连不上或协议校验失败，返回 503（服务不可用），并带上错误原因
        raise HTTPException(status_code=503, detail=f"MCP Server 验证失败：{exc}") from exc

    initialize = inspection.get("initialize") or {}  # 取握手阶段返回的初始化信息（服务器名、版本、协议版本）
    server_info = initialize.get("serverInfo") or {}  # 从初始化信息里取服务器基本信息
    tools = [  # 把 MCP 服务器返回的原始工具列表，逐条转成对外模型
        McpToolResponse(
            name=str(item.get("name") or ""),  # 工具名
            description=str(item.get("description") or ""),  # 工具描述
            input_schema=item.get("inputSchema") or {},  # 工具的入参 JSON Schema
        )
        # 遍历 MCP 返回的工具数组（没有就当空列表）
        for item in inspection.get("tools") or []
    ]
    return McpRuntimeResponse(
        status="online",  # 能走到这里说明连上了，状态置为 online
        server_name=str(server_info.get("name") or "unknown"),  # 服务器名字
        server_version=str(server_info.get("version") or "unknown"),  # 服务器版本
        protocol_version=str(initialize.get("protocolVersion") or "unknown"),  # 协议版本
        transport="stdio",  # 传输方式：通过标准输入输出通信
        tools=tools,  # 上面整理好的工具列表
    )


@router.get("/{skill_name}", response_model=SkillResponse)  # GET /api/skills/{skill_name}，路径里带技能名；返回单个 SkillResponse
async def get_skill(skill_name: str) -> SkillResponse:
    # 处理「查看单个技能详情」的请求，skill_name 是路径参数（比如 /api/skills/issue_agent）
    registry = get_skill_registry()  # 拿注册表
    skill = registry.get_skill(skill_name)  # 按名字查技能
    if skill is None:
        # 没找到就返回 404（资源不存在）
        raise HTTPException(status_code=404, detail="未找到 Skill")
    # 找到后返回详情，这里 include_instructions=True 把完整使用说明一起返回
    return _skill_response(skill, include_instructions=True)
