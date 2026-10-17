"""DevMind AI module."""

# 正则表达式库，用于从文本里提取术语/技术词
import re
# Any：任意类型占位
from typing import Any

# 导入分析输出模型：Issue 分析结果、草稿对象
from app.schemas.analysis import IssueAnalysisOutput, IssueDrafts
# 导入 Agent 基类
from app.services.agents.base import BaseAgent
# 导入大模型客户端封装（用来调用 LLM）
from app.services.llm.client import LLMClient
# 导入 ISSUE 分析的提示词模板（写给 LLM 的系统/用户提示）
from app.services.llm.prompts import ISSUE_ANALYSIS_PROMPT


# 模块说明（给零基础同事看的）：
# IssueAgent 是「GitHub Issue 分诊专家」。
# 它接收一个 Issue（标题、正文、标签等），自动判断：这是什么类型（Bug/需求/文档…）、优先级（P0~P3）、
# 复杂度、建议交给谁、是不是重复 Issue、该不该澄清/拆分/关闭，并给出行动清单和草稿评论。
# 输入：一个 Issue 数据（dict，含 title/body/labels/assignees 等）+ 上下文（团队、相似 Issue、代码线索等）。
# 输出：一份 IssueAnalysisOutput（含结论、优先级、负责人、证据、checklist、草稿等）。
# 被谁调用：通常由 WorkflowOrchestrator 在计划里派发 issue_analysis 任务时调用；也可被 ChatAgent 的 issue_agent.analyze_issue 工具调用。
# 典型调用链：GitHub 同步 Issue → IssueAgent.run() → 先本地规则兜底，再用 LLM 增强 → 合并输出。
# 为什么这样写：先用「本地规则」算出一份 fallback（兜底）结论，再交给 LLM；LLM 结果和兜底合并，
#               这样即使 LLM 不可用/出错，也能给出一个基本可用的分诊结论（更安全、更可控）。
class IssueAgent(BaseAgent):
    """IssueAgent."""
    name = "issue_analyst_agent"
    description = "分析 GitHub Issue 的分诊结论、优先级、复杂度、负责人、证据和下一步动作。"
    # 声明它能用的工具（这些都是 GitHub 检索/上下文类工具，提供分析所需原料）
    available_tools = ["get_issue_detail", "search_similar_issues", "search_related_code", "search_project_docs", "read_thread_context"]

    def __init__(self, llm: LLMClient | None = None) -> None:
        # 保存大模型客户端；没传就新建一个默认的
        self.llm = llm or LLMClient()

    # 主入口：先算本地兜底分析，再尝试用 LLM 增强，最后合并返回
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 先得到一份「纯规则」的兜底分析（不依赖 LLM，永远有结果）
        fallback = self._fallback_analysis(input_data, context)
        # 调用 LLM 让它基于提示词 + Issue/上下文 产出结构化 JSON
        llm_output = await self.llm.chat_json(ISSUE_ANALYSIS_PROMPT, {"issue": input_data, "context": context})
        if llm_output:
            try:
                # 把 LLM 输出和兜底合并（LLM 优先，但兜底补缺失字段），再校验成模型对象
                normalized = self._merge_with_fallback(llm_output, fallback, input_data, context)
                return IssueAnalysisOutput.model_validate(normalized).model_dump()
            except Exception:
                # LLM 输出不合法就静默忽略，退回兜底（保证不崩溃）
                pass
        return fallback

    # 纯本地规则的分诊分析：不调用 LLM，靠关键词 + 启发式规则得出结论
    def _fallback_analysis(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 把标题+正文拼成小写文本，便于关键词匹配
        text = f"{input_data.get('title', '')} {input_data.get('body', '')}".lower()
        body = str(input_data.get("body") or "")  # 原始正文（用于算置信度）
        labels = [label.lower() for label in input_data.get("labels", [])]  # 标签转小写
        # 分类：Bug / 文档 / 测试 / 运维 / 重构 / 需求 / 咨询
        category = self._classify_category(text, labels)
        # 优先级：P0~P3
        priority = self._classify_priority(text, labels)
        # 复杂度：S / M / L / XL
        complexity = self._estimate_complexity(text, labels)
        # 建议负责人（及原因）
        owner, owner_reason = self._suggest_owner(input_data, text, context)
        # 重复候选（已校验过的）
        duplicates = self._validated_duplicates(context.get("duplicate_candidates", []))
        # 结论：进入开发 / 等待澄清 / 拆分 / 合并 / 关闭
        conclusion, conclusion_reason = self._choose_conclusion(input_data, text, labels, category, complexity, duplicates)
        # 行动清单
        checklist = self._build_checklist(conclusion, category)
        # 草稿评论 / 拆解草稿
        drafts = self._build_drafts(input_data, conclusion, checklist)
        # 证据收集
        evidence = self._collect_evidence(input_data, context)
        # 置信度评分
        confidence = self._estimate_confidence(body, evidence, duplicates, owner)
        # 组装成 IssueAnalysisOutput 并转字典
        return IssueAnalysisOutput(
            summary=self._build_summary(input_data, conclusion),
            conclusion=conclusion,
            conclusion_reason=conclusion_reason,
            category=category,
            priority=priority,
            complexity=complexity,
            suggested_owner=owner,
            owner_reason=owner_reason,
            duplicate_candidates=duplicates,
            evidence=evidence,
            checklist=checklist,
            action_items=checklist,
            drafts=drafts,
            confidence=confidence,
        ).model_dump()

    # 把 LLM 输出和兜底合并：LLM 有的字段覆盖兜底，兜底补 LLM 缺的；并对关键字段做校验/修正
    def _merge_with_fallback(self, output: dict[str, Any], fallback: dict[str, Any], input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 先整体用兜底打底，再用 LLM 输出覆盖（{**fallback, **output} 后者覆盖前者）
        merged = {**fallback, **output}
        # checklist 和 action_items 互为兜底：缺哪个用另一个补
        if not merged.get("checklist") and merged.get("action_items"):
            merged["checklist"] = merged["action_items"]
        if not merged.get("action_items") and merged.get("checklist"):
            merged["action_items"] = merged["checklist"]
        # 证据缺失就用兜底证据，并去重
        if not merged.get("evidence"):
            merged["evidence"] = fallback["evidence"]
        merged["evidence"] = self._dedupe_evidence(merged.get("evidence"))
        # 重复候选缺失用兜底
        if not merged.get("duplicate_candidates"):
            merged["duplicate_candidates"] = fallback["duplicate_candidates"]
        # drafts 不是字典就用兜底
        if not isinstance(merged.get("drafts"), dict):
            merged["drafts"] = fallback["drafts"]
        # 负责人合法性校验：不在允许名单里就退回兜底
        if not self._is_allowed_owner(str(merged.get("suggested_owner") or ""), input_data, context):
            merged["suggested_owner"] = fallback["suggested_owner"]
            merged["owner_reason"] = fallback["owner_reason"]
        # 如果 LLM 给的是泛化负责人（如 "backend"）而兜底有具体人，优先用兜底的具体人
        if self._should_prefer_fallback_owner(str(merged.get("suggested_owner") or ""), str(fallback.get("suggested_owner") or "")):
            merged["suggested_owner"] = fallback["suggested_owner"]
            merged["owner_reason"] = fallback["owner_reason"]
        # 如果 LLM 误判成 Question，但兜底认定是明确的技术类别，就用兜底类别
        if merged.get("category") == "Question" and fallback.get("category") in {"Bug", "Feature", "Documentation", "Ops", "Refactor", "Test"}:
            merged["category"] = fallback["category"]
        return merged

    # 判断是否需要优先用兜底负责人：兜底是具体人，而 LLM 给的是泛化词（空/待分配/backend 等）
    def _should_prefer_fallback_owner(self, current_owner: str, fallback_owner: str) -> bool:
        generic_owners = {"", "待分配", "unassigned", "backend", "frontend", "ci", "docs"}
        return fallback_owner not in generic_owners and current_owner.lower() in generic_owners

    # 校验负责人是否被允许：空/待分配 直接放行；否则必须在「现有 assignees + 团队成员」名单里
    def _is_allowed_owner(self, owner: str, input_data: dict[str, Any], context: dict[str, Any]) -> bool:
        normalized_owner = owner.strip().lower()
        # 未分配类直接视为合法（说明当前无法确定负责人）
        if normalized_owner in {"", "待分配", "unassigned"}:
            return True
        # 允许名单 = Issue 现有 assignees + 团队成员名字
        allowed = {str(item).strip().lower() for item in input_data.get("assignees") or [] if str(item).strip()}
        members = context.get("team_members") or []
        if isinstance(members, list):
            for member in members:
                if isinstance(member, dict) and str(member.get("name") or "").strip():
                    allowed.add(str(member.get("name")).strip().lower())
        return normalized_owner in allowed

    # 生成一句话总结
    def _build_summary(self, input_data: dict[str, Any], conclusion: str) -> str:
        number = input_data.get("number")
        title = str(input_data.get("title") or "Untitled Issue").strip()
        prefix = f"Issue #{number}" if number else "Issue"
        return f"{prefix} {title} 的建议结论是：{conclusion}。"

    # 分类：按标签+正文关键词匹配规则，命中第一个即返回；都没命中默认 "Feature"
    def _classify_category(self, text: str, labels: list[str]) -> str:
        joined = " ".join(labels) + " " + text  # 标签和正文拼一起
        rules = [
            ("Bug", ["bug", "error", "fail", "500", "crash", "exception", "broken", "regression"]),
            ("Documentation", ["doc", "documentation", "readme", "guide", "文档", "说明"]),
            ("Test", ["test", "pytest", "coverage", "单测", "测试"]),
            ("Ops", ["ops", "ci", "workflow", "deploy", "docker", "infra", "环境", "权限"]),
            ("Refactor", ["refactor", "cleanup", "重构", "technical debt"]),
            ("Feature", ["feature", "enhancement", "proposal", "需求", "新增"]),
            ("Question", ["question", "how to", "why does", "咨询", "疑问"]),
        ]
        for category, tokens in rules:
            if any(token in joined for token in tokens):
                return category
        return "Feature"

    # 优先级：依次判断 P0 / P1 / P3，都没命中默认 P2
    def _classify_priority(self, text: str, labels: list[str]) -> str:
        joined = " ".join(labels) + " " + text
        if any(token in joined for token in ["p0", "sev0", "critical", "security", "data loss", "数据丢失", "全量不可用"]):
            return "P0"
        if any(token in joined for token in ["p1", "sev1", "crash", "cannot login", "500", "生产", "阻塞", "blocker"]):
            return "P1"
        if any(token in joined for token in ["p3", "minor", "polish", "typo", "文案", "低优"]):
            return "P3"
        return "P2"

    # 复杂度：XL / L / S / M 的启发式判断
    def _estimate_complexity(self, text: str, labels: list[str]) -> str:
        joined = " ".join(labels) + " " + text
        if any(token in joined for token in ["xl", "architecture", "migration", "rewrite", "跨模块", "架构"]):
            return "XL"
        if any(token in joined for token in ["large", "complex", "integration", "schema", "多模块"]):
            return "L"
        # 很短且像文案/文档类 → S
        if len(text) < 280 and any(token in joined for token in ["typo", "copy", "文案", "doc"]):
            return "S"
        # 否则按长度粗略分：短是 M，长是 L
        return "M" if len(text) < 1400 else "L"

    # 建议负责人：先看是否已有 assignee（沿用），否则尝试从团队画像匹配，再否则按方向推断但仍标记「待分配」
    def _suggest_owner(self, input_data: dict[str, Any], issue_text: str, context: dict[str, Any]) -> tuple[str, str]:
        assignees = input_data.get("assignees") or []
        if assignees:
            # 已经有指派 → 直接沿用第一个，最可靠
            owner = str(assignees[0])
            return owner, f"Issue 已分配给 {owner}，优先沿用现有负责人。"

        # 拼出用于匹配的「信号文本」（Issue 文本 + 代码/文档/会话证据）
        signal_text = self._owner_signal_text(issue_text, context)
        # 尝试根据团队画像匹配
        team_owner, team_reason = self._suggest_owner_from_team(issue_text, signal_text, context)
        if team_owner:
            return team_owner, team_reason or "团队画像与 Issue 关键词匹配。"

        # 推断方向（backend/frontend/ci/docs）
        area = self._infer_area(signal_text)
        members = context.get("team_members") or []
        if not isinstance(members, list) or not members:
            # 没配团队：如果方向明确就说明「需要先加成员」，否则也说需要先加成员
            if area != "待分配":
                return "待分配", f"当前项目还没有添加团队成员；这个 Issue 更像 {area} 方向，请先在团队面板添加对应开发成员后再分配。"
            return "待分配", "当前项目还没有添加团队成员，请先在团队面板添加开发成员后再分配。"
        if area != "待分配":
            # 配了团队但没匹配上具体人 → 提示补充画像
            return "待分配", f"未找到与 {area} 方向匹配的团队成员，请先补充团队成员画像后再分配。"
        return "待分配", "当前 Issue 与团队画像、代码/文档证据的匹配度不足，需要人工确认负责人。"

    # 从团队成员画像里匹配负责人：按关键词命中打分，分最高的成员胜出
    def _suggest_owner_from_team(self, issue_text: str, signal_text: str, context: dict[str, Any]) -> tuple[str | None, str | None]:
        members = context.get("team_members") or []
        if not isinstance(members, list):
            return None, None

        best_name = None
        best_score = 0
        best_reasons: list[str] = []
        for member in members:
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            # 把成员的 role/strengths/techStack 拼成一段画像文本（小写）
            profile = " ".join(
                str(member.get(field) or "").lower()
                for field in ["role", "strengths", "techStack", "tech_stack"]
            )
            score = 0
            reasons: list[str] = []
            # 画像里的术语命中 Issue 信号 → 每个 +1 分
            for term in self._profile_terms(profile):
                if len(term) >= 2 and term in signal_text:
                    score += 1
                    reasons.append(term)
            # 命中「领域规则」（如桌面化/后端/前端/CI），一次性 +8 分（权重更高）
            for domain, issue_terms, profile_terms in self._owner_domain_rules():
                if any(term in signal_text for term in issue_terms) and any(term in profile for term in profile_terms):
                    score += 8
                    reasons.append(domain)
            # 分更高就更新最佳匹配
            if name and score > best_score:
                best_name = name
                best_score = score
                best_reasons = reasons
        if best_name and best_score > 0:
            # 去重 reasons 取前 4 个，拼成可读的匹配原因
            reason_text = "、".join(dict.fromkeys(best_reasons[:4])) or "团队画像"
            return best_name, f"团队成员画像与 Issue/代码/文档信号匹配：{reason_text}，匹配分 {best_score}。"
        return None, None

    # 推断 Issue 大致方向（backend / frontend / ci / docs / 待分配）
    def _infer_area(self, issue_text: str) -> str:
        area_keywords = {
            "backend": ["api", "database", "token", "server", "fastapi", "postgres", "auth", "jwt"],
            "frontend": ["ui", "page", "button", "react", "next", "css", "layout", "browser", "页面", "按钮", "electron", "desktop", "桌面化", "桌面端"],
            "ci": ["ci", "workflow", "actions", "pytest", "lint", "build", "deploy"],
            "docs": ["readme", "doc", "文档", "说明"],
        }
        for area, keywords in area_keywords.items():
            if any(keyword in issue_text for keyword in keywords):
                return area
        return "待分配"

    # 把代码引用、项目文档、会话证据等 context 内容拼进信号文本，并对桌面化相关做额外加权关键词
    def _owner_signal_text(self, issue_text: str, context: dict[str, Any]) -> str:
        parts = [issue_text]
        for key in ["code_references", "project_documents", "conversation_evidence"]:
            items = context.get(key)
            if not isinstance(items, list):
                continue
            for item in items[:6]:  # 每种最多取前 6 条，控制信号规模
                if isinstance(item, dict):
                    parts.extend([str(item.get("title") or ""), str(item.get("snippet") or ""), str(item.get("content") or "")])
        text = " ".join(parts).lower()
        # 桌面化是本项目重要方向，命中就补一批相关关键词，提升匹配权重
        if any(term in text for term in ["桌面化", "桌面端", "桌面应用", "electron"]):
            text += " electron desktop windows macos javascript node frontend"
        return text

    # 把成员画像文本切成一个个术语 token（中英文/数字/符号），用于关键词匹配
    def _profile_terms(self, profile: str) -> list[str]:
        normalized = profile.replace("擅长", " ").replace("熟悉", " ").replace("技术栈", " ")
        return re.findall(r"[a-zA-Z0-9_+#.\u4e00-\u9fff]+", normalized.lower())

    # 定义「领域规则」：每个领域 = (领域名, Issue 信号词, 成员画像词)
    def _owner_domain_rules(self) -> list[tuple[str, list[str], list[str]]]:
        return [
            (
                "桌面化/electron",
                ["桌面化", "桌面端", "桌面应用", "electron", "desktop", "windows", "macos", "native app"],
                ["electron", "desktop", "桌面", "windows", "macos", "tauri", "javascript", "js", "node", "frontend", "前端"],
            ),
            (
                "后端/API",
                ["api", "database", "token", "server", "fastapi", "postgres", "auth", "jwt"],
                ["api", "database", "server", "fastapi", "postgres", "auth", "jwt", "后端"],
            ),
            (
                "前端/UI",
                ["ui", "page", "button", "react", "next", "css", "layout", "browser", "页面", "按钮"],
                ["ui", "react", "next", "css", "browser", "frontend", "前端"],
            ),
            (
                "CI/工程化",
                ["ci", "workflow", "actions", "pytest", "lint", "build", "deploy"],
                ["ci", "workflow", "actions", "pytest", "lint", "build", "deploy", "工程化"],
            ),
        ]

    # 校验重复候选：过滤非法项，并把相似度分数夹在 [0,1]
    def _validated_duplicates(self, candidates: Any) -> list[dict[str, Any]]:
        if not isinstance(candidates, list):
            return []
        rows = []
        for item in candidates[:5]:  # 最多取前 5 个
            if not isinstance(item, dict):
                continue
            issue_id = str(item.get("issue_id") or item.get("source_id") or "").strip()
            title = str(item.get("title") or "").strip()
            try:
                score = max(0.0, min(1.0, float(item.get("score") or 0)))  # 相似度夹到 [0,1]
            except (TypeError, ValueError):
                score = 0.0
            if issue_id and title:
                rows.append({"issue_id": issue_id, "title": title, "score": score})
        return rows

    # 决定结论：关闭 / 合并到已有 Issue / 拆分 / 等待澄清 / 进入开发
    def _choose_conclusion(
        self,
        input_data: dict[str, Any],
        text: str,
        labels: list[str],
        category: str,
        complexity: str,
        duplicates: list[dict[str, Any]],
    ) -> tuple[str, str]:
        state = str(input_data.get("state") or "").lower()
        joined = " ".join(labels) + " " + text
        # 已关闭或带无效/不处理标签 → 建议关闭归档
        if state == "closed" or any(token in joined for token in ["invalid", "wontfix", "won't fix", "not planned", "无效", "不处理"]):
            return "关闭", "Issue 已关闭或带有无效/不处理信号，建议归档并保留关闭说明。"
        # 有高相似度（>=0.86）的重复 Issue → 建议合并
        if any(float(item.get("score") or 0) >= 0.86 for item in duplicates):
            return "合并到已有 Issue", "相似历史 Issue 的匹配度较高，建议合并上下文，避免重复推进。"
        # 超大复杂度或明显多目标 → 建议拆分
        if complexity in {"XL"} or self._looks_like_multi_scope(text):
            return "拆分", "Issue 范围偏大或包含多个独立目标，拆分后更容易排期和验收。"
        # 信息不全（Bug/需求缺关键描述）→ 建议先澄清
        if self._needs_clarification(text, category):
            return "等待澄清", "当前描述缺少影响范围、复现/验收条件或期望结果，直接进入开发风险较高。"
        # 默认：信息基本可用 → 进入开发
        return "进入开发", "Issue 信息基本可执行，优先确认验收口径后进入开发。"

    # 判断是否是「多目标」Issue：分隔符过多或含「同时/以及/跨模块」等词
    def _looks_like_multi_scope(self, text: str) -> bool:
        separators = text.count("\n-") + text.count("\n*") + text.count("、") + text.count(";")
        broad_terms = ["同时", "以及", "并且", "多个", "all of", "migration", "architecture", "跨模块"]
        return separators >= 5 or any(term in text for term in broad_terms)

    # 判断是否需要澄清：太短，或 Bug/需求缺关键描述要素
    def _needs_clarification(self, text: str, category: str) -> bool:
        if len(text.strip()) < 80:
            return True  # 太短基本说不清
        if category == "Bug":
            # Bug 需要「复现步骤/期望/实际/影响」相关信息
            has_repro = any(token in text for token in ["reproduce", "steps", "复现", "步骤", "expected", "actual", "期望", "实际"])
            has_impact = any(token in text for token in ["impact", "影响", "scope", "范围", "blocking", "阻塞"])
            return not (has_repro or has_impact)
        if category == "Feature":
            # 需求需要「验收标准/价值/场景」
            has_acceptance = any(token in text for token in ["acceptance", "验收", "criteria", "why", "value", "价值", "场景"])
            return not has_acceptance and len(text.strip()) < 220
        return False

    # 收集证据：Issue 本身 + 相似 Issue + 代码/文档/会话证据 + 团队成员画像
    def _collect_evidence(self, input_data: dict[str, Any], context: dict[str, Any]) -> list[dict[str, Any]]:
        evidence = [
            {
                "source_type": "issue",
                "title": f"Issue #{input_data.get('number')}: {input_data.get('title')}",
                "snippet": self._clip("\n".join(filter(None, [str(input_data.get("title") or ""), str(input_data.get("body") or "")])), 320),
                "reference": str(input_data.get("id") or input_data.get("number") or ""),
                "confidence": 1.0,  # Issue 本体证据置信度最高
            }
        ]
        # 把已校验的相似 Issue 作为证据（前 3 个）
        for duplicate in self._validated_duplicates(context.get("duplicate_candidates", []))[:3]:
            evidence.append(
                {
                    "source_type": "similar_issue",
                    "title": duplicate["title"],
                    "snippet": f"相似度 {duplicate['score']:.2f}，可作为重复/历史处理经验参考。",
                    "reference": duplicate["issue_id"],
                    "confidence": duplicate["score"],
                }
            )
        # 三类上下文（代码/文档/会话）各自转成证据
        for key, source_type in [
            ("code_references", "code"),
            ("project_documents", "document"),
            ("conversation_evidence", "conversation"),
        ]:
            evidence.extend(self._evidence_from_context(context.get(key), source_type))
        # 团队成员也可作为「负责人分配」的证据
        evidence.extend(self._team_evidence(context.get("team_members")))
        # 去重并最多保留 10 条
        return self._dedupe_evidence(evidence)[:10]

    # 证据去重：按 (类型, 引用, 标题, 片段前180字) 去重，且要求片段非空
    def _dedupe_evidence(self, items: Any) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        rows: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str, str]] = set()
        for item in items:
            if not isinstance(item, dict) or not item.get("snippet"):
                continue
            key = (
                str(item.get("source_type") or ""),
                str(item.get("reference") or ""),
                str(item.get("title") or ""),
                str(item.get("snippet") or "")[:180],
            )
            if key in seen:
                continue
            seen.add(key)
            rows.append(item)
        return rows

    # 把一类 context 列表项转成证据字典
    def _evidence_from_context(self, items: Any, default_source_type: str) -> list[dict[str, Any]]:
        if not isinstance(items, list):
            return []
        rows = []
        for item in items[:4]:  # 每种最多 4 条
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("path") or item.get("source_id") or default_source_type).strip()
            snippet = self._clip(str(item.get("snippet") or item.get("content") or ""), 320)
            if not snippet:
                continue
            score = item.get("score")
            try:
                confidence = max(0.0, min(1.0, float(score))) if score is not None else None
            except (TypeError, ValueError):
                confidence = None
            rows.append(
                {
                    "source_type": str(item.get("source_type") or default_source_type),
                    "title": title,
                    "snippet": snippet,
                    "reference": str(item.get("reference") or item.get("source_id") or item.get("id") or ""),
                    "confidence": confidence,
                }
            )
        return rows

    # 把团队成员转成 evidence（用于负责人分配依据）
    def _team_evidence(self, members: Any) -> list[dict[str, Any]]:
        if not isinstance(members, list) or not members:
            return []
        rows = []
        for member in members[:3]:  # 最多 3 个
            if not isinstance(member, dict):
                continue
            name = str(member.get("name") or "").strip()
            if not name:
                continue
            # 把 role/strengths/techStack 拼成一段画像摘要
            profile = "；".join(
                str(member.get(field) or "").strip()
                for field in ["role", "strengths", "techStack", "tech_stack"]
                if str(member.get(field) or "").strip()
            )
            rows.append(
                {
                    "source_type": "team_member",
                    "title": f"团队成员：{name}",
                    "snippet": profile or "团队成员资料可作为负责人分配依据。",
                    "reference": name,
                    "confidence": None,  # 成员画像本身不带置信度
                }
            )
        return rows

    # 根据结论类型生成对应的行动清单（checklist）
    def _build_checklist(self, conclusion: str, category: str) -> list[str]:
        if conclusion == "等待澄清":
            return [
                "请补充问题背景、影响范围和用户场景。",
                "Bug 类问题补充复现步骤、期望结果、实际结果和日志/截图。",
                "Feature 类需求补充验收标准和非目标范围。",
                "澄清后重新运行 Issue Agent 分析。",
            ]
        if conclusion == "合并到已有 Issue":
            return [
                "确认相似 Issue 是否覆盖当前诉求。",
                "把当前 Issue 的新增信息整理为评论追加到已有 Issue。",
                "为当前 Issue 标记 duplicate/关联关系。",
                "经人工确认后关闭当前重复 Issue。",
            ]
        if conclusion == "拆分":
            return [
                "按用户价值、模块边界或交付顺序拆成 2-5 个子任务。",
                "为每个子任务补充验收标准、依赖关系和建议负责人。",
                "确认最小可交付范围，优先排第一个可独立验证的子任务。",
                "把拆分说明作为评论回填到原 Issue。",
            ]
        if conclusion == "关闭":
            return [
                "确认关闭原因是无效、重复、已完成还是不计划处理。",
                "补充一条简短关闭评论，说明依据和替代路径。",
                "经人工确认后更新标签或关闭 Issue。",
            ]
        # 默认（进入开发）基线清单
        baseline = [
            "确认 Issue 验收标准和不做范围。",
            "定位相关代码/文档位置并补充设计备注。",
            "创建开发分支并实现最小修复/功能增量。",
            "补充自动化测试或手工验证步骤。",
            "提交 PR 并在描述中关联该 Issue。",
        ]
        if category == "Bug":
            # Bug 类额外强调先写复现测试
            baseline.insert(2, "先写一个能复现问题的失败测试或最小复现脚本。")
        return baseline

    # 生成草稿：澄清评论草稿、任务拆解草稿（按结论类型）
    def _build_drafts(self, input_data: dict[str, Any], conclusion: str, checklist: list[str]) -> IssueDrafts:
        number = input_data.get("number")
        title = str(input_data.get("title") or "该 Issue")
        clarification = None
        task_breakdown = None
        if conclusion == "等待澄清":
            clarification = (
                f"针对 Issue #{number} {title}，目前信息还不足以直接进入开发。"
                "请补充：影响范围、复现步骤/使用场景、期望结果、实际结果，以及是否有日志、截图或相关版本信息。"
            )
        if conclusion in {"拆分", "进入开发"}:
            task_breakdown = "建议下一步拆解：\n" + "\n".join(f"- {item}" for item in checklist[:5])
        if conclusion == "合并到已有 Issue":
            task_breakdown = (
                f"建议将 Issue #{number} 的新增信息合并到相似历史 Issue，并在当前 Issue 留下 duplicate 说明后再关闭。"
            )
        return IssueDrafts(clarification_comment=clarification, task_breakdown=task_breakdown)

    # 估算置信度：基于正文长度、证据数量、重复项、负责人是否明确，累加得分并夹在 [0.35, 0.86]
    def _estimate_confidence(self, body: str, evidence: list[dict[str, Any]], duplicates: list[dict[str, Any]], owner: str) -> float:
        score = 0.45
        if len(body.strip()) >= 120:
            score += 0.12
        if len(evidence) >= 3:
            score += 0.14
        if duplicates:
            score += 0.08
        if owner != "待分配":
            score += 0.08
        return round(max(0.35, min(0.86, score)), 2)

    # 工具函数：把文本截断到 limit 长度，超长就在末尾加省略号
    def _clip(self, text: str, limit: int) -> str:
        text = (text or "").strip()
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"
