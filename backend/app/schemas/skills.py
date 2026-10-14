"""
skills.py —— 技能（Skills）/MCP 工具接口的数据契约（Schema 层）

本文件服务于「查看可用技能与 MCP 工具」接口：
- SkillManifestResponse：一个技能的「清单/说明书」（名字、能力、触发条件等）；
- SkillResponse / SkillListResponse：单个/批量技能（含实现路径与指令文本）；
- McpToolResponse / McpRuntimeResponse：MCP（模型上下文协议）工具及其运行时信息。

技能是一段可复用的 Agent 流程；MCP 是让 Agent 调用外部工具的标准协议。."""

from typing import Any  # Any：任意类型，用于工具入参 schema

from pydantic import BaseModel, Field  # BaseModel：基类；Field：字段约束


class SkillManifestResponse(BaseModel):
    """SkillManifestResponse."""
    # 一个技能的「说明书」结构
    name: str  # 技能唯一名，必填
    title: str  # 展示标题，必填
    version: str  # 版本号，必填
    description: str  # 功能描述，必填
    category: str  # 分类，必填
    entrypoint: str  # 入口标识（被调用时的名字），必填
    tools: list[str] = Field(default_factory=list)  # 该技能用到的工具列表，默认空
    input_modes: list[str] = Field(default_factory=list)  # 支持的输入模式，默认空
    triggers: list[str] = Field(default_factory=list)  # 自动触发条件列表，默认空
    workflow_steps: list[str] = Field(default_factory=list)  # 工作流步骤描述，默认空
    output_contract: str  # 输出契约（返回格式说明），必填
    safety_level: str  # 安全等级，必填


class SkillResponse(BaseModel):
    """SkillResponse."""
    # 单个技能（说明书 + 实现细节）
    manifest: SkillManifestResponse  # 说明书对象，必填
    path: str  # 技能文件所在路径，必填
    instructions: str | None = None  # 技能指令文本；可空（如未加载）


class SkillListResponse(BaseModel):
    """SkillListResponse."""
    # 技能列表响应
    skills: list[SkillResponse]  # 技能数组，必填


class McpToolResponse(BaseModel):
    """McpToolResponse."""
    # MCP 中一个工具的响应结构
    name: str  # 工具名，必填
    description: str  # 工具描述，必填
    input_schema: dict[str, Any] = Field(default_factory=dict)  # 入参 JSON Schema，默认空


class McpRuntimeResponse(BaseModel):
    """McpRuntimeResponse."""
    # MCP 运行时的响应结构
    status: str  # 运行状态，必填
    server_name: str  # 服务名，必填
    server_version: str  # 服务版本，必填
    protocol_version: str  # 协议版本，必填
    transport: str  # 传输方式（stdio/sse…），必填
    tools: list[McpToolResponse] = Field(default_factory=list)  # 该运行时暴露的工具列表，默认空
