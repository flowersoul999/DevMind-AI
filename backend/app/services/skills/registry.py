"""
services/skills/registry.py —— Skill（技能）注册表：扫描、解析、激活
============================================================================

项目里的「Skill」就是一段「专家工作流说明书」，写在每个 skills/<名字>/SKILL.md 里。
这个文件负责把它们管理起来：

1. 扫描 skills/ 目录下所有 SKILL.md，解析开头的 frontmatter（YAML 风格元数据）和正文；
2. 校验名称/入口不重复；
3. 当收到用户消息时，判断该「激活」哪些 Skill（明确指定 or 关键词命中触发条件）；
4. 激活后生成「提示词块」和「追踪元数据」，供 ChatAgent 把 Skill 说明喂给 LLM。

为什么搞这套：把「某个领域怎么做」沉淀成可复用、可审计的 Skill 文件，LLM 在合适场景
自动套用，而不是每次都靠 prompt 硬编码。."""

import ast  # 安全地解析列表字面量
import hashlib  # 算内容摘要（digest），用于校验/追踪
import re  # 正则：解析 frontmatter、匹配触发词、抽取引用资源
from functools import lru_cache  # 缓存：注册表只构建一次
from pathlib import Path  # 路径
from typing import Any  # 任意类型

from pydantic import BaseModel, Field  # 数据模型


class SkillRegistryError(ValueError):
    """SkillRegistryError."""
    # 技能注册相关的错误（继承 ValueError，表示「参数/配置不对」）
    pass


class SkillManifest(BaseModel):
    """SkillManifest."""
    # SKILL.md 开头 frontmatter 解析成的「清单」结构（技能的自描述元数据）
    name: str  # 技能唯一名（英文）
    title: str  # 显示标题
    version: str  # 版本
    description: str  # 一句话描述
    category: str = "general"  # 分类，默认 general
    entrypoint: str  # 入口名（对应 LLM 的工具名）
    tools: list[str] = Field(default_factory=list)  # 这个技能允许用的工具
    input_modes: list[str] = Field(default_factory=list)  # 输入模式
    triggers: list[str] = Field(default_factory=list)  # 触发关键词（命中就自动激活）
    workflow_steps: list[str] = Field(default_factory=list)  # 工作流步骤
    output_contract: str = "freeform"  # 输出契约（freeform/structured 等）
    safety_level: str = "read_only"  # 安全级别（read_only/write 等）


class SkillDefinition(BaseModel):
    """SkillDefinition."""
    # 一个完整技能定义 = 清单 + 文件路径 + 正文说明
    manifest: SkillManifest  # 上面的元数据
    path: str  # SKILL.md 的绝对路径
    instructions: str  # SKILL.md 正文（给 LLM 的步骤说明）

    def trace_metadata(self) -> dict[str, Any]:
        # 抽出可用于「追踪/审计」的元数据（评测或日志用）
        return {
            "skill_name": self.manifest.name,
            "skill_title": self.manifest.title,
            "skill_version": self.manifest.version,
            "skill_steps": self.manifest.workflow_steps,
        }

    def prompt_summary(self) -> str:
        # 生成一行简短摘要，列在「可用技能」清单里
        steps = ", ".join(self.manifest.workflow_steps) if self.manifest.workflow_steps else "在 SKILL.md 中定义"
        return (
            f"- {self.manifest.name} v{self.manifest.version} -> {self.manifest.entrypoint}: "
            f"{self.manifest.description} 步骤：{steps}。安全级别：{self.manifest.safety_level}。"
        )


class SkillActivation(BaseModel):
    """SkillActivation."""
    # 一次「激活」的结果：把技能和这次激活的原因、上下文打包
    skill_name: str
    skill_title: str
    skill_version: str
    entrypoint: str
    tools: list[str] = Field(default_factory=list)
    workflow_steps: list[str] = Field(default_factory=list)
    output_contract: str
    safety_level: str
    activation_mode: str  # explicit（明确指定）/ automatic（自动命中）/ entrypoint（专用入口）
    activation_reason: str  # 为什么激活
    instructions: str  # 喂给 LLM 的正文
    instruction_digest: str  # 正文摘要（前 16 位 sha256），用于比对/追踪
    resources: list[dict[str, Any]] = Field(default_factory=list)  # 按需加载的引用资源

    def trace_metadata(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "skill_title": self.skill_title,
            "skill_version": self.skill_version,
            "skill_steps": self.workflow_steps,
            "skill_activation_mode": self.activation_mode,
            "skill_activation_reason": self.activation_reason,
            "skill_instruction_digest": self.instruction_digest,
        }

    def prompt_block(self) -> str:
        # 生成「塞进 LLM 提示词」的完整技能块（标题、激活信息、允许工具、正文、引用资源）
        resource_text = ""
        if self.resources:
            sections = []
            for resource in self.resources:
                content = str(resource.get("content") or "").strip()
                if content:
                    # 每个引用资源用 markdown 标题标出路径 + 内容
                    sections.append(f"### 资源：{resource['path']}\n{content}")
            if sections:
                resource_text = "\n\n按需加载的 Skill 资源：\n\n" + "\n\n".join(sections)
        return (
            f"## {self.skill_title} ({self.skill_name} v{self.skill_version})\n"
            f"激活方式：{self.activation_mode}；原因：{self.activation_reason}\n"
            f"允许工具：{', '.join(self.tools) or '无'}\n"
            f"输出契约：{self.output_contract}；安全级别：{self.safety_level}\n\n"
            f"{self.instructions.strip()}"
            f"{resource_text}"
        )


class SkillRegistry:
    # 技能注册表主体：加载所有技能、提供查询/激活能力
    def __init__(self, root: Path) -> None:
        self.root = root  # skills 目录根
        self._skills = self._load_skills()  # 加载全部技能定义
        self._validate_unique_keys()  # 校验名称/入口唯一
        # 建两个索引：按名字、按入口（entrypoint）快速查
        self._by_name = {skill.manifest.name: skill for skill in self._skills}
        self._by_entrypoint = {skill.manifest.entrypoint: skill for skill in self._skills}

    def list_skills(self) -> list[SkillDefinition]:
        return sorted(self._skills, key=lambda skill: skill.manifest.name)  # 按名字排序返回

    def get_skill(self, name: str) -> SkillDefinition | None:
        return self._by_name.get(name)  # 按名字查

    def skill_for_entrypoint(self, tool_name: str) -> SkillDefinition | None:
        return self._by_entrypoint.get(tool_name)  # 按入口（工具名）查

    def trace_metadata_by_tool(self) -> dict[str, dict[str, Any]]:
        # 给所有入口生成追踪元数据，键是工具名
        return {tool_name: skill.trace_metadata() for tool_name, skill in self._by_entrypoint.items()}

    def annotate_tool_call(self, tool_call: dict[str, Any]) -> dict[str, Any]:
        # 给一次「工具调用」记录补上它所属的 skill 元数据（用于评测追踪）
        tool_name = str(tool_call.get("tool_name") or "")
        skill = self.skill_for_entrypoint(tool_name)
        if not skill:
            return dict(tool_call)  # 不属于任何 skill 就原样返回
        return {**tool_call, **skill.trace_metadata()}  # 合并追踪元数据

    def prompt_context(self) -> str:
        # 生成「系统提示词里列出所有可用技能」的段落
        skills = self.list_skills()
        if not skills:
            return "当前没有注册 DevFlow 技能。"
        return "\n".join(skill.prompt_summary() for skill in skills)

    def tool_description_suffix(self, tool_name: str) -> str:
        # 给某个工具描述追加「它属于哪个已注册技能」
        skill = self.skill_for_entrypoint(tool_name)
        if not skill:
            return ""
        return (
            f" 注册技能：{skill.manifest.title} v{skill.manifest.version}。"
            f"{skill.manifest.description}"
        )

    def tool_description_suffixes(self) -> dict[str, str]:
        # 给所有入口生成「工具名 -> 追加描述」
        return {tool_name: self.tool_description_suffix(tool_name) for tool_name in self._by_entrypoint}

    def activate(
        self,
        message: str,
        *,
        requested_name: str | None = None,
        max_auto_skills: int = 3,
    ) -> list[SkillActivation]:
        # 核心：根据消息决定激活哪些技能
        explicit = self._explicit_skills(message, requested_name)  # 先看用户是否明确点名
        if explicit:
            # 明确指定就激活它（mode=explicit），不再自动匹配
            return [self._activation(skill, "explicit", "用户明确指定 Skill") for skill in explicit]

        scored = [
            (self._activation_score(message, skill), skill)
            for skill in self._skills
        ]
        # 只保留得分 >0 的（命中了触发条件）
        selected = [(score, skill) for score, skill in scored if score > 0]
        selected.sort(key=lambda item: (-item[0], item[1].manifest.name))  # 按分降序
        # 最多自动激活 max_auto_skills 个（默认 3）
        return [
            self._activation(skill, "automatic", f"消息命中 Skill 触发条件，匹配分 {score}")
            for score, skill in selected[:max_auto_skills]
        ]

    def activate_entrypoint(self, tool_name: str, *, reason: str = "专用 Agent 执行入口") -> SkillActivation | None:
        # 按入口名直接激活（比如某 Agent 专用入口）
        skill = self.skill_for_entrypoint(tool_name)
        if skill is None:
            return None
        return self._activation(skill, "entrypoint", reason)

    def _activation(self, skill: SkillDefinition, mode: str, reason: str) -> SkillActivation:
        # 构造一个 SkillActivation：算正文摘要、加载引用资源
        instructions = skill.instructions.strip()
        return SkillActivation(
            skill_name=skill.manifest.name,
            skill_title=skill.manifest.title,
            skill_version=skill.manifest.version,
            entrypoint=skill.manifest.entrypoint,
            tools=skill.manifest.tools,
            workflow_steps=skill.manifest.workflow_steps,
            output_contract=skill.manifest.output_contract,
            safety_level=skill.manifest.safety_level,
            activation_mode=mode,
            activation_reason=reason,
            instructions=instructions,
            # 用 sha256 取正文前 16 位十六进制作为摘要，省空间又能比对
            instruction_digest=hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:16],
            resources=self._load_referenced_resources(skill),  # 加载 SKILL.md 里引用的 references 等资源
        )

    def _explicit_skills(self, message: str, requested_name: str | None) -> list[SkillDefinition]:
        # 判断用户是否「明确点了某个技能名」
        if requested_name:
            skill = self._skill_by_name_or_title(requested_name)
            if skill is None:
                raise SkillRegistryError(f"未找到用户指定的 Skill：{requested_name}")
            return [skill]
        # 消息里没出现「skill/技能」字样就认为没有点名
        lowered = message.lower()
        if "skill" not in lowered and "技能" not in message:
            return []
        # 出现 skill 字样：看消息里是否含某个技能的名字或标题
        selected = [
            skill
            for skill in self._skills
            if skill.manifest.name.lower() in lowered or skill.manifest.title.lower() in lowered
        ]
        if selected:
            return selected
        # 再试正则：匹配「用/使用/调用/启用 xxx skill」这类句式
        match = re.search(
            r"(?:use|using|用|使用|调用|启用)\s*([A-Za-z][A-Za-z0-9_-]+)\s*(?:skill|技能)",
            message,
            flags=re.IGNORECASE,
        )
        if match:
            raise SkillRegistryError(f"未找到用户指定的 Skill：{match.group(1)}")  # 点了但找不到，报错
        return []

    def _skill_by_name_or_title(self, value: str) -> SkillDefinition | None:
        # 按名字或标题精确匹配（忽略大小写）
        normalized = value.strip().lower()
        return next(
            (
                skill
                for skill in self._skills
                if skill.manifest.name.lower() == normalized or skill.manifest.title.lower() == normalized
            ),
            None,
        )

    def _activation_score(self, message: str, skill: SkillDefinition) -> int:
        # 算「消息命中这个技能触发词」的得分：每个命中触发词加分（词越长分越高，封顶 8）
        lowered = message.lower()
        score = 0
        for trigger in skill.manifest.triggers:
            normalized = trigger.strip().lower()
            if not normalized:
                continue
            # 纯标识符式触发词用「单词边界」匹配（避免 "ci" 误中 "city"）；其它用包含匹配
            if re.fullmatch(r"[a-z0-9_+#.-]+", normalized):
                matched = re.search(rf"(?<![a-z0-9_]){re.escape(normalized)}(?![a-z0-9_])", lowered)
            else:
                matched = normalized in lowered
            if matched:
                score += max(1, min(len(normalized), 8))
        return score

    def _load_referenced_resources(self, skill: SkillDefinition) -> list[dict[str, Any]]:
        # 从 SKILL.md 正文里抽出被引用的资源（如 `references/risk-checklist.md`），读内容并校验
        # 正则找 `references/...` `templates/...` `scripts/...` `assets/...` 形式的引用
        references = sorted(
            {
                match.group(1).strip()
                for match in re.finditer(
                    r"`((?:references|templates|scripts|assets)/[^`]+)`",
                    skill.instructions,
                )
            }
        )
        if len(references) > 8:
            raise SkillRegistryError(f"Skill {skill.manifest.name} 引用的资源超过 8 个")  # 防滥用
        skill_root = Path(skill.path).resolve().parent  # 技能所在目录
        resources: list[dict[str, Any]] = []
        text_suffixes = {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".sh", ".ps1"}  # 这些当文本读
        for relative in references:
            resource_path = (skill_root / relative).resolve()
            try:
                resource_path.relative_to(skill_root)  # 路径穿越防护：资源必须在技能目录内
            except ValueError as exc:
                raise SkillRegistryError(f"Skill {skill.manifest.name} 引用了目录外资源：{relative}") from exc
            if not resource_path.is_file():
                raise SkillRegistryError(f"Skill {skill.manifest.name} 引用的资源不存在：{relative}")
            if resource_path.stat().st_size > 64 * 1024:
                raise SkillRegistryError(f"Skill {skill.manifest.name} 引用的资源超过 64 KiB：{relative}")  # 太大不加载
            content = resource_path.read_text(encoding="utf-8") if resource_path.suffix.lower() in text_suffixes else ""
            resources.append(
                {
                    "path": relative.replace("\\", "/"),
                    "content": content,
                    "size": resource_path.stat().st_size,
                    "digest": hashlib.sha256(resource_path.read_bytes()).hexdigest()[:16],
                }
            )
        return resources

    def _load_skills(self) -> list[SkillDefinition]:
        # 扫描 root 下所有 `*/SKILL.md`，逐个加载
        if not self.root.exists():
            return []
        skills: list[SkillDefinition] = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            skills.append(_load_skill(skill_file))
        return skills

    def _validate_unique_keys(self) -> None:
        # 校验所有技能 name 和 entrypoint 不重复，否则抛错（重复会导致激活歧义）
        seen_names: set[str] = set()
        seen_entrypoints: set[str] = set()
        for skill in self._skills:
            if skill.manifest.name in seen_names:
                raise SkillRegistryError(f"Skill 名称重复：{skill.manifest.name}")
            if skill.manifest.entrypoint in seen_entrypoints:
                raise SkillRegistryError(f"Skill entrypoint 重复：{skill.manifest.entrypoint}")
            seen_names.add(skill.manifest.name)
            seen_entrypoints.add(skill.manifest.entrypoint)


def _load_skill(path: Path) -> SkillDefinition:
    # 读一个 SKILL.md：拆 frontmatter 和正文，校验成 SkillManifest，组装 SkillDefinition
    text = path.read_text(encoding="utf-8")
    manifest_data, instructions = _split_frontmatter(text, path)
    manifest = SkillManifest.model_validate(manifest_data)  # Pydantic 校验 + 填默认值
    return SkillDefinition(manifest=manifest, path=str(path), instructions=instructions)


def _split_frontmatter(text: str, path: Path) -> tuple[dict[str, Any], str]:
    # 把 SKILL.md 拆成 (frontmatter字典, 正文)。frontmatter 是文件开头 `---` 到 `---` 之间的部分
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")  # 统一换行符
    if not lines or lines[0].strip() != "---":
        raise SkillRegistryError(f"{path} 必须以 frontmatter 开头")  # 必须以 --- 开头
    frontmatter_lines: list[str] = []
    body_start = None
    for index, line in enumerate(lines[1:], start=1):
        if line.strip() == "---":
            body_start = index + 1  # 找到结束的 ---，正文从下一行开始
            break
        frontmatter_lines.append(line)
    if body_start is None:
        raise SkillRegistryError(f"{path} 缺少 frontmatter 结束标记")
    return _parse_frontmatter(frontmatter_lines), "\n".join(lines[body_start:]).strip()


def _parse_frontmatter(lines: list[str]) -> dict[str, Any]:
    # 解析 frontmatter 的每一行，支持 `key: value` 和列表项 `- xxx`
    data: dict[str, Any] = {}
    current_list_key: str | None = None  # 当前正在收集列表的父 key
    for raw_line in lines:
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue  # 空行和注释跳过
        if stripped.startswith("- "):
            # 列表项：必须挂在某 key 下
            if current_list_key is None:
                raise SkillRegistryError("列表项缺少父级 key")
            data.setdefault(current_list_key, [])
            if not isinstance(data[current_list_key], list):
                raise SkillRegistryError(f"{current_list_key} 不是列表")
            data[current_list_key].append(_parse_value(stripped[2:]))  # 去掉 "- " 后解析值
            continue
        if ":" not in raw_line:
            raise SkillRegistryError(f"frontmatter 行无效：{raw_line}")
        key, value = raw_line.split(":", 1)  # 只按第一个冒号分
        key = key.strip()
        value = value.strip()
        if not key:
            raise SkillRegistryError("frontmatter key 不能为空")
        if value == "":
            # 空值表示开始一个列表（下面可能跟 - 项）
            data[key] = []
            current_list_key = key
        else:
            data[key] = _parse_value(value)
            current_list_key = None
    return data


def _parse_value(value: str) -> Any:
    # 把 frontmatter 里的字符串值转成合适的 Python 类型
    value = value.strip()
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        # 方括号当列表：优先用 ast.literal_eval 安全解析，失败就手动按逗号拆
        try:
            parsed = ast.literal_eval(value)
        except (SyntaxError, ValueError):
            parsed = [item.strip().strip("'\"") for item in value[1:-1].split(",") if item.strip()]
        return parsed if isinstance(parsed, list) else value
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]  # 去引号
    return value  # 其余当普通字符串


@lru_cache(maxsize=1)
def get_skill_registry() -> SkillRegistry:
    # 全局单例：注册表构建一次并缓存（lru_cache 保证只算一次）
    # parents[2] 从 services/skills/registry.py 往上两级到 backend/，再进 skills/
    root = Path(__file__).resolve().parents[2] / "skills"
    return SkillRegistry(root)
