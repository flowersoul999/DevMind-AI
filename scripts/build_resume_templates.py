"""DevMind AI module."""

from __future__ import annotations

import re
from pathlib import Path
from zipfile import ZipFile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_COLOR_INDEX, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Mm, Pt, RGBColor


ROOT = Path(__file__).resolve().parents[1]
OUTPUT_DIR = ROOT / "docs" / "resume-templates"

# compact_reference_guide preset with a named resume_profile override:
# A4 page, tighter margins, Chinese UI font, black section tabs, and no running footer.
FONT_LATIN = "Arial"
FONT_CJK = "Microsoft YaHei"
BLACK = RGBColor(20, 20, 20)
MUTED = RGBColor(92, 92, 92)
PLACEHOLDER = RGBColor(110, 79, 0)


TEMPLATES = [
    {
        "filename": "DevFlow_AI_简历模板_校招_AI应用开发.docx",
        "name": "【姓名】",
        "contact": "【手机号】 | 【邮箱】 | GitHub: 【链接】 | 【所在城市】",
        "headline": "应届生 | AI 应用开发工程师 | 【本科/硕士 · 毕业年份】",
        "sections": [
            {
                "title": "教育背景",
                "blocks": [
                    {
                        "type": "row",
                        "left": "【学校名称】",
                        "middle": "【计算机相关专业】（【本科/硕士】）",
                        "right": "【2022.09 - 2026.06】",
                    },
                    {"type": "bullet", "text": "GPA【X.XX/4.0】；专业排名【前 X%】；【奖学金 / 竞赛 / 荣誉，无则删除】。"},
                    {"type": "bullet", "text": "主修课程：【数据结构、计算机网络、操作系统、数据库、人工智能等，仅保留高分或能展开的课程】。"},
                ],
            },
            {
                "title": "专业技能",
                "blocks": [
                    {"type": "bullet", "text": "熟悉 Python 与面向对象编程，能够使用 FastAPI、Pydantic、SQLAlchemy 完成接口、数据校验与持久化开发。"},
                    {"type": "bullet", "text": "熟悉大模型 Tool Calling 与 Agent Loop，了解 LangChain / LangGraph 的工具编排、状态管理、终止条件与结构化输出。"},
                    {"type": "bullet", "text": "熟悉 RAG 全流程，掌握文档解析、结构感知切片、Embedding、向量 / 关键词混合检索、重排与证据引用。"},
                    {"type": "bullet", "text": "了解 PostgreSQL、Milvus、Redis 等数据组件，能够使用 Docker Compose 搭建本地开发与演示环境。"},
                    {"type": "bullet", "text": "了解 Next.js、TypeScript、React Query 与 SSE，可完成 Agent 状态、工具轨迹和流式结果的前端展示。"},
                ],
            },
            {
                "title": "实习经历",
                "blocks": [
                    {
                        "type": "row",
                        "left": "【公司 / 实验室名称；无实习可删除本节】",
                        "middle": "【AI 应用开发实习生】",
                        "right": "【起止时间】",
                    },
                    {"type": "bullet", "text": "负责【具体模块】，围绕【业务问题】完成【设计 / 开发 / 测试】，交付【可验证结果】。"},
                    {"type": "bullet", "text": "通过【优化措施】将【准确率 / 响应耗时 / 人工处理量】从【基线】改善至【结果】，样本量为【X】。"},
                ],
            },
            {
                "title": "项目经历",
                "blocks": [
                    {
                        "type": "row",
                        "left": "DevMind AI：GitHub 研发协作 Agent",
                        "middle": "AI 应用开发",
                        "right": "【起止时间】",
                    },
                    {
                        "type": "label",
                        "label": "项目简介：",
                        "text": "面向 GitHub Issue、PR 与 CI 场景的全栈 Agent 系统，将仓库事实、本地代码、项目文档和会话记忆组织为可核验的工程证据，并生成结构化分析与受控操作草稿。",
                    },
                    {
                        "type": "label",
                        "label": "技术栈：",
                        "text": "Python、FastAPI、LangChain、LangGraph、PostgreSQL、Milvus、Next.js、TypeScript、Docker Compose。",
                    },
                    {"type": "bullet", "text": "Agent 编排：实现 ChatAgent 工具循环，并为 Issue 分诊、PR 审查、CI 排障拆分专用 Agent；通过 12 步上限、重复调用保护与可重试错误分类约束失控循环。"},
                    {"type": "bullet", "text": "多 Agent 工作流：搭建 Planner -> 专用 Agent -> Observer -> Synthesis 中心化链路，支持任务依赖、并行、超时与有限 replan，并输出可追踪的中间状态。"},
                    {"type": "bullet", "text": "RAG 链路：支持 TXT、Markdown、PDF、DOCX、JSON、CSV 与日志等 7 类文件，完成结构感知切片、向量 / 关键词混合检索、重排、召回测试和编号引用。"},
                    {"type": "bullet", "text": "安全治理：将模型建议与外部写入解耦，通过 ActionDraft、轻量 RBAC、人工确认和 AuditLog 控制评论、标签等副作用。"},
                    {"type": "bullet", "text": "工程验证：以 22 个后端测试模块、143 项自动化测试覆盖 Agent、RAG、上下文压缩、Skill 与工作流等关键行为，当前套件全部通过；【补充召回指标或演示数据】。"},
                ],
            },
            {
                "title": "个人总结",
                "blocks": [
                    {"type": "bullet", "text": "能够从业务场景出发拆解 Agent、RAG、安全与评测边界，重视结构化输出、证据引用和可验证交付。"},
                    {"type": "bullet", "text": "【补充一条与你真实经历对应的团队协作、竞赛、开源或技术写作证据】。"},
                ],
            },
        ],
    },
    {
        "filename": "DevFlow_AI_简历模板_社招_AI应用工程.docx",
        "name": "【姓名】",
        "contact": "【手机号】 | 【邮箱】 | GitHub: 【链接】 | 【所在城市】",
        "headline": "【X 年经验】 | AI 应用开发工程师 | 【最高学历 · 毕业年份】",
        "sections": [
            {
                "title": "专业技能",
                "blocks": [
                    {"type": "bullet", "text": "熟悉大模型应用工程，具备 Tool Calling、Agent Loop、任务路由、结构化输出、上下文预算与会话记忆的落地经验。"},
                    {"type": "bullet", "text": "掌握 RAG 工程链路，包括多格式解析、结构感知切片、混合检索、rerank、证据引用、召回评测与索引重建。"},
                    {"type": "bullet", "text": "熟悉 Python、FastAPI、Pydantic、SQLAlchemy、PostgreSQL 与 Milvus，能够设计异步 API、数据模型和仓库级隔离。"},
                    {"type": "bullet", "text": "熟悉 LangChain / LangGraph 与 OpenAI 兼容模型 API，能够处理工具白名单、调用上限、重试、降级与成本观测。"},
                    {"type": "bullet", "text": "了解 Next.js、TypeScript、React Query 与 SSE，可与前端协作呈现工具轨迹、Agent 状态、证据和流式响应。"},
                ],
            },
            {
                "title": "工作经历",
                "blocks": [
                    {
                        "type": "row",
                        "left": "【公司名称】",
                        "middle": "【AI 应用 / 后端工程师】",
                        "right": "【起止时间】",
                    },
                    {"type": "bullet", "text": "负责【产品 / 平台】的【模块范围】，从需求澄清、方案设计到上线运维完成闭环。"},
                    {"type": "bullet", "text": "围绕【检索质量 / 响应性能 / 稳定性 / Token 成本】建立基线并落地【具体优化】，将【指标】改善【X%】。"},
                    {"type": "bullet", "text": "推动【接口规范 / 评测集 / Code Review / 监控告警】落地，减少【缺陷 / 回归时间 / 人工修改量】至【结果】。"},
                ],
            },
            {
                "title": "项目经历",
                "blocks": [
                    {
                        "type": "row",
                        "left": "DevFlow AI：研发协作 Agent 平台",
                        "middle": "核心开发 / 架构设计",
                        "right": "【起止时间】",
                    },
                    {
                        "type": "label",
                        "label": "项目简介：",
                        "text": "面向研发团队的 AI 协作平台，同步 GitHub Issue、PR、Review、Workflow Run 与失败日志，结合当前代码、RAG 和会话记忆完成分诊、审查、排障与工程决策。",
                    },
                    {
                        "type": "label",
                        "label": "技术栈：",
                        "text": "FastAPI、LangChain、LangGraph、PostgreSQL、Milvus、Next.js、TypeScript、Docker Compose。",
                    },
                    {"type": "bullet", "text": "Agent 架构：保留 ChatAgent 统一入口，按 Issue、PR、CI 拆分输入证据与输出 Schema；跨领域任务进入 Planner、Observer、Synthesis 组成的中心化任务图。"},
                    {"type": "bullet", "text": "上下文工程：区分仓库事实、检索证据、会话记忆与执行状态，支持会话封存、结构化记忆、候选审批、按需召回与压缩标记，降低状态混用风险。"},
                    {"type": "bullet", "text": "RAG 可靠性：实现仓库隔离、metadata filter、向量 / 关键词混合检索、rerank 与 Milvus / 数据库 fallback；通过 source policy 将当前代码与实时状态留在权威数据源核验。"},
                    {"type": "bullet", "text": "安全写入：建立模型提议 -> 工具 Schema -> 风险判断 -> ActionDraft -> 角色权限 -> 人工确认 -> GitHub API -> AuditLog 链路，避免模型直接执行副作用。"},
                    {"type": "bullet", "text": "质量闭环：沉淀 AgentRun、任务轨迹和 EvalRun，以 143 项自动化测试覆盖工具事件顺序、重复调用、工作流依赖、超时、replan、上下文压缩与结构化输出。"},
                    {"type": "bullet", "text": "业务结果：【仅填写真实试点数据，例如分析接受率、人工修改率、任务耗时、错误写入率、单任务 Token 成本；暂无数据时删除本条】。"},
                    {
                        "type": "row",
                        "left": "【第二个与岗位强相关的项目】",
                        "middle": "【你的角色】",
                        "right": "【起止时间】",
                    },
                    {"type": "bullet", "text": "【用“问题 - 方案 - 结果”写 2 至 4 条，不要只罗列技术名词】。"},
                ],
            },
            {
                "title": "自我评价",
                "blocks": [
                    {"type": "bullet", "text": "具备【X】年【AI 应用 / 后端】经验，能够独立推进从需求、架构、开发到验证的完整交付。"},
                    {"type": "bullet", "text": "重视工程边界和事实证据，能清楚说明方案取舍、当前限制与下一步生产化路径。"},
                ],
            },
        ],
    },
    {
        "filename": "DevFlow_AI_简历模板_社招_后端Agent平台.docx",
        "name": "【姓名】",
        "contact": "【手机号】 | 【邮箱】 | GitHub: 【链接】 | 【所在城市】",
        "headline": "【X 年经验】 | Python 后端 / Agent 平台工程师 | 【最高学历 · 毕业年份】",
        "sections": [
            {
                "title": "专业技能",
                "blocks": [
                    {"type": "bullet", "text": "熟悉 Python、FastAPI、Pydantic、SQLAlchemy 与异步编程，可完成 API 分层、数据建模、异常治理和 SSE 流式接口开发。"},
                    {"type": "bullet", "text": "熟悉 PostgreSQL、pgvector 与 Milvus，具备关系数据、向量索引、metadata filter、仓库隔离和 fallback 方案设计经验。"},
                    {"type": "bullet", "text": "熟悉 GitHub REST API 与研发协作对象，能够同步 Issue、PR、Review、Workflow Run、Job 和失败日志并建立内部事实模型。"},
                    {"type": "bullet", "text": "掌握 Agent 工具循环、任务 DAG、超时与有限重规划，理解结构化输出、幂等、审计、权限控制和人工确认的重要性。"},
                    {"type": "bullet", "text": "熟悉 Pytest 与 Agent Eval，能够围绕工具协议、任务依赖、RAG 召回、上下文压缩和安全边界编写回归测试。"},
                    {"type": "bullet", "text": "了解 Next.js、TypeScript、React Query、Tailwind CSS 与 Docker Compose，具备全栈联调和本地演示环境搭建能力。"},
                ],
            },
            {
                "title": "工作经历",
                "blocks": [
                    {
                        "type": "row",
                        "left": "【公司名称】",
                        "middle": "【后端 / 平台工程师】",
                        "right": "【起止时间】",
                    },
                    {"type": "bullet", "text": "负责【平台 / 服务】的后端研发与架构演进，覆盖【接口、数据、任务、权限、监控】等模块。"},
                    {"type": "bullet", "text": "针对【稳定性 / 性能 / 数据一致性】问题完成【具体改造】，将【指标】从【基线】优化至【结果】。"},
                    {"type": "bullet", "text": "参与【Code Review / 故障复盘 / 测试门禁 / 发布流程】，推动【可量化质量结果】。"},
                ],
            },
            {
                "title": "项目经历",
                "blocks": [
                    {
                        "type": "row",
                        "left": "DevFlow AI：全栈研发协作平台",
                        "middle": "后端 / Agent 平台开发",
                        "right": "【起止时间】",
                    },
                    {
                        "type": "label",
                        "label": "项目简介：",
                        "text": "围绕 GitHub 研发协作构建的全栈 Agent 系统，统一承接仓库同步、研发事实建模、代码与知识检索、Agent 编排、安全写入、执行轨迹和评测回归。",
                    },
                    {
                        "type": "label",
                        "label": "技术栈：",
                        "text": "Python、FastAPI、Pydantic、SQLAlchemy、PostgreSQL、Milvus、LangGraph、Next.js、TypeScript。",
                    },
                    {"type": "bullet", "text": "数据底座：设计 Repository、Issue、PullRequest、WorkflowRun、Document、EvidenceItem、AgentRun 等领域模型，并通过 repo / conversation 过滤约束查询边界。"},
                    {"type": "bullet", "text": "研发同步：对接 GitHub API，同步 Issue、PR 文件、Review 评论、Workflow Run、Job 与日志，为分诊、审查和 CI 排障提供结构化事实。"},
                    {"type": "bullet", "text": "检索边界：非结构化知识进入 RAG，当前代码走工作区词法检索，团队与实时状态走结构化查询；通过 source policy 和 reindex 清理过期来源，避免旧代码污染召回。"},
                    {"type": "bullet", "text": "任务编排：实现带依赖、并行、超时、失败跳过与有限 replan 的中心化任务图，Observer 检查冲突与证据缺口，Synthesis 汇总最终工程结论。"},
                    {"type": "bullet", "text": "安全治理：基于角色权限与工具白名单限制能力边界，外部写操作先生成 ActionDraft，确认后执行并写入审计记录。"},
                    {"type": "bullet", "text": "可观测与验证：通过 SSE 输出 tool_use / tool_result 与 Agent 状态，22 个后端测试模块、143 项自动化测试覆盖 Agent、RAG、Skill、索引和工作流关键行为。"},
                    {"type": "bullet", "text": "规模结果：【填写真实数据，如同步对象规模、P95 延迟、任务成功率、故障恢复时间；无法证明时删除本条】。"},
                    {
                        "type": "row",
                        "left": "【第二个后端 / 平台项目】",
                        "middle": "【你的角色】",
                        "right": "【起止时间】",
                    },
                    {"type": "bullet", "text": "【突出数据库、并发、缓存、消息、稳定性或工程效率中的一到两个主题，并给出真实指标】。"},
                ],
            },
            {
                "title": "自我评价",
                "blocks": [
                    {"type": "bullet", "text": "具备后端系统与 AI Agent 的交叉能力，能把模型能力落到可控、可追踪、可回归的工程链路中。"},
                    {"type": "bullet", "text": "习惯从数据边界、失败恢复、安全副作用和测试证据审视系统设计，而不是只关注模型最终回答。"},
                ],
            },
        ],
    },
]


def set_font(run, *, size: float, bold: bool = False, color: RGBColor = BLACK) -> None:
    run.font.name = FONT_LATIN
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), FONT_CJK)
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.color.rgb = color


def set_cell_shading(paragraph, fill: str) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    shading = p_pr.find(qn("w:shd"))
    if shading is None:
        shading = OxmlElement("w:shd")
        p_pr.append(shading)
    shading.set(qn("w:fill"), fill)
    shading.set(qn("w:val"), "clear")


def set_keep_with_next(paragraph) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    if p_pr.find(qn("w:keepNext")) is None:
        p_pr.append(OxmlElement("w:keepNext"))


def add_page_number(paragraph) -> None:
    paragraph.alignment = WD_ALIGN_PARAGRAPH.RIGHT
    run = paragraph.add_run("第 ")
    set_font(run, size=8, color=MUTED)
    fld_char_1 = OxmlElement("w:fldChar")
    fld_char_1.set(qn("w:fldCharType"), "begin")
    instr_text = OxmlElement("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = "PAGE"
    fld_char_2 = OxmlElement("w:fldChar")
    fld_char_2.set(qn("w:fldCharType"), "end")
    run._r.append(fld_char_1)
    run._r.append(instr_text)
    run._r.append(fld_char_2)
    suffix = paragraph.add_run(" 页")
    set_font(suffix, size=8, color=MUTED)


def add_placeholder_runs(paragraph, text: str, *, size: float = 9.3, bold: bool = False, color: RGBColor = BLACK) -> None:
    parts = re.split(r"(【[^】]+】)", text)
    for part in parts:
        if not part:
            continue
        is_placeholder = part.startswith("【") and part.endswith("】")
        run = paragraph.add_run(part)
        set_font(run, size=size, bold=bold, color=PLACEHOLDER if is_placeholder else color)
        if is_placeholder:
            run.font.highlight_color = WD_COLOR_INDEX.YELLOW


def add_numbering(document: Document) -> int:
    numbering = document.part.numbering_part.element
    abstract_ids = [int(item.get(qn("w:abstractNumId"))) for item in numbering.findall(qn("w:abstractNum"))]
    num_ids = [int(item.get(qn("w:numId"))) for item in numbering.findall(qn("w:num"))]
    abstract_id = max(abstract_ids, default=0) + 1
    num_id = max(num_ids, default=0) + 1

    abstract = OxmlElement("w:abstractNum")
    abstract.set(qn("w:abstractNumId"), str(abstract_id))
    multi = OxmlElement("w:multiLevelType")
    multi.set(qn("w:val"), "singleLevel")
    abstract.append(multi)
    level = OxmlElement("w:lvl")
    level.set(qn("w:ilvl"), "0")
    start = OxmlElement("w:start")
    start.set(qn("w:val"), "1")
    num_fmt = OxmlElement("w:numFmt")
    num_fmt.set(qn("w:val"), "bullet")
    lvl_text = OxmlElement("w:lvlText")
    lvl_text.set(qn("w:val"), "•")
    lvl_jc = OxmlElement("w:lvlJc")
    lvl_jc.set(qn("w:val"), "left")
    level.extend([start, num_fmt, lvl_text, lvl_jc])

    p_pr = OxmlElement("w:pPr")
    tabs = OxmlElement("w:tabs")
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "num")
    tab.set(qn("w:pos"), "540")
    tabs.append(tab)
    indent = OxmlElement("w:ind")
    indent.set(qn("w:left"), "540")
    indent.set(qn("w:hanging"), "270")
    spacing = OxmlElement("w:spacing")
    spacing.set(qn("w:after"), "30")
    spacing.set(qn("w:line"), "270")
    spacing.set(qn("w:lineRule"), "auto")
    p_pr.extend([tabs, indent, spacing])
    level.append(p_pr)
    abstract.append(level)
    numbering.append(abstract)

    num = OxmlElement("w:num")
    num.set(qn("w:numId"), str(num_id))
    abstract_ref = OxmlElement("w:abstractNumId")
    abstract_ref.set(qn("w:val"), str(abstract_id))
    num.append(abstract_ref)
    numbering.append(num)
    return num_id


def apply_bullet(paragraph, num_id: int) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    num_pr = p_pr.find(qn("w:numPr"))
    if num_pr is None:
        num_pr = OxmlElement("w:numPr")
        p_pr.append(num_pr)
    ilvl = OxmlElement("w:ilvl")
    ilvl.set(qn("w:val"), "0")
    num = OxmlElement("w:numId")
    num.set(qn("w:val"), str(num_id))
    num_pr.extend([ilvl, num])


def configure_document(document: Document) -> int:
    section = document.sections[0]
    section.page_width = Mm(210)
    section.page_height = Mm(297)
    section.top_margin = Mm(12.7)
    section.bottom_margin = Mm(12.7)
    section.left_margin = Mm(16)
    section.right_margin = Mm(16)
    section.header_distance = Mm(7)
    section.footer_distance = Mm(7)

    normal = document.styles["Normal"]
    normal.font.name = FONT_LATIN
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CJK)
    normal.font.size = Pt(9.3)
    normal.font.color.rgb = BLACK
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(1.5)
    normal.paragraph_format.line_spacing = 1.12

    section_style = document.styles.add_style("Resume Section", WD_STYLE_TYPE.PARAGRAPH)
    section_style.font.name = FONT_LATIN
    section_style._element.rPr.rFonts.set(qn("w:eastAsia"), FONT_CJK)
    section_style.font.size = Pt(10.3)
    section_style.font.bold = True
    section_style.font.color.rgb = RGBColor(255, 255, 255)
    section_style.paragraph_format.space_before = Pt(5)
    section_style.paragraph_format.space_after = Pt(3)
    section_style.paragraph_format.left_indent = Mm(0)
    section_style.paragraph_format.right_indent = Mm(142)
    section_style.paragraph_format.keep_with_next = True

    footer = section.footer.paragraphs[0]
    footer.paragraph_format.space_before = Pt(0)
    footer.paragraph_format.space_after = Pt(0)
    add_page_number(footer)
    return add_numbering(document)


def add_header(document: Document, template: dict) -> None:
    name = document.add_paragraph()
    name.alignment = WD_ALIGN_PARAGRAPH.CENTER
    name.paragraph_format.space_after = Pt(1.5)
    add_placeholder_runs(name, template["name"], size=19, bold=True)

    contact = document.add_paragraph()
    contact.alignment = WD_ALIGN_PARAGRAPH.CENTER
    contact.paragraph_format.space_after = Pt(1)
    add_placeholder_runs(contact, template["contact"], size=8.4, color=MUTED)

    headline = document.add_paragraph()
    headline.alignment = WD_ALIGN_PARAGRAPH.CENTER
    headline.paragraph_format.space_after = Pt(3.5)
    add_placeholder_runs(headline, template["headline"], size=9.1, bold=True, color=MUTED)


def add_section_heading(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(style="Resume Section")
    set_cell_shading(paragraph, "161616")
    run = paragraph.add_run(text)
    set_font(run, size=10.3, bold=True, color=RGBColor(255, 255, 255))
    set_keep_with_next(paragraph)


def add_row(document: Document, left: str, middle: str, right: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(0.5)
    paragraph.paragraph_format.space_after = Pt(1)
    paragraph.paragraph_format.keep_with_next = True
    tabs = paragraph.paragraph_format.tab_stops
    tabs.add_tab_stop(Mm(113), WD_TAB_ALIGNMENT.CENTER)
    tabs.add_tab_stop(Mm(178), WD_TAB_ALIGNMENT.RIGHT)
    add_placeholder_runs(paragraph, left, size=9.3, bold=True)
    paragraph.add_run("\t")
    add_placeholder_runs(paragraph, middle, size=9.0, bold=True)
    paragraph.add_run("\t")
    add_placeholder_runs(paragraph, right, size=8.8, color=MUTED)


def add_label(document: Document, label: str, text: str) -> None:
    paragraph = document.add_paragraph()
    paragraph.paragraph_format.space_after = Pt(1.5)
    label_run = paragraph.add_run(label)
    set_font(label_run, size=9.2, bold=True)
    add_placeholder_runs(paragraph, text, size=9.2)


def add_bullet(document: Document, text: str, num_id: int) -> None:
    paragraph = document.add_paragraph()
    apply_bullet(paragraph, num_id)
    paragraph.paragraph_format.space_after = Pt(1.5)
    paragraph.paragraph_format.line_spacing = 1.12
    prefix, separator, remainder = text.partition("：")
    if separator and len(prefix) <= 12 and "【" not in prefix:
        run = paragraph.add_run(prefix + separator)
        set_font(run, size=9.15, bold=True)
        add_placeholder_runs(paragraph, remainder, size=9.15)
    else:
        add_placeholder_runs(paragraph, text, size=9.15)


def add_block(document: Document, block: dict, num_id: int) -> None:
    block_type = block["type"]
    if block_type == "row":
        add_row(document, block["left"], block["middle"], block["right"])
    elif block_type == "label":
        add_label(document, block["label"], block["text"])
    elif block_type == "bullet":
        add_bullet(document, block["text"], num_id)
    else:
        raise ValueError(f"Unknown block type: {block_type}")


def build_template(template: dict) -> Path:
    document = Document()
    num_id = configure_document(document)
    add_header(document, template)
    for section in template["sections"]:
        add_section_heading(document, section["title"])
        for block in section["blocks"]:
            add_block(document, block, num_id)

    properties = document.core_properties
    properties.title = template["filename"].removesuffix(".docx")
    properties.subject = "DevFlow AI 项目简历模板"
    properties.author = ""
    properties.last_modified_by = ""
    properties.keywords = "DevFlow AI, AI Agent, RAG, Resume"

    output = OUTPUT_DIR / template["filename"]
    document.save(output)
    validate_output(output, template)
    return output


def validate_output(output: Path, template: dict) -> None:
    document = Document(output)
    text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    expected_sections = [section["title"] for section in template["sections"]]
    missing = [title for title in expected_sections if title not in text]
    if missing:
        raise AssertionError(f"Missing sections in {output.name}: {missing}")
    if "DevFlow AI" not in text or "【" not in text:
        raise AssertionError(f"Missing project copy or placeholders in {output.name}")
    if document.tables:
        raise AssertionError(f"Resume uses layout tables: {output.name}")

    section = document.sections[0]
    if abs(section.page_width.mm - 210) > 0.2 or abs(section.page_height.mm - 297) > 0.2:
        raise AssertionError(f"Unexpected page geometry in {output.name}")
    if abs(section.left_margin.mm - 16) > 0.2 or abs(section.right_margin.mm - 16) > 0.2:
        raise AssertionError(f"Unexpected side margins in {output.name}")

    bullet_count = sum(
        1
        for paragraph in document.paragraphs
        if paragraph._p.get_or_add_pPr().find(qn("w:numPr")) is not None
    )
    if bullet_count < 10:
        raise AssertionError(f"Too few real list items in {output.name}: {bullet_count}")

    with ZipFile(output) as package:
        document_xml = package.read("word/document.xml").decode("utf-8")
        numbering_xml = package.read("word/numbering.xml").decode("utf-8")
    if 'w:fill="161616"' not in document_xml:
        raise AssertionError(f"Missing section tab shading in {output.name}")
    if "w:numFmt w:val=\"bullet\"" not in numbering_xml:
        raise AssertionError(f"Missing explicit bullet numbering in {output.name}")

    print(
        f"validated {output.name}: "
        f"paragraphs={len(document.paragraphs)}, bullets={bullet_count}, "
        f"placeholders={text.count('【')}"
    )


def main() -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for template in TEMPLATES:
        print(build_template(template))


if __name__ == "__main__":
    main()
