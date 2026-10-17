"""DevMind AI module."""

# Any：任意类型占位
from typing import Any

# 导入分析输出模型：PR 审查结果
from app.schemas.analysis import PRReviewOutput
# 导入 Agent 基类
from app.services.agents.base import BaseAgent
# 导入大模型客户端
from app.services.llm.client import LLMClient
# 导入 PR 审查提示词模板
from app.services.llm.prompts import PR_REVIEW_PROMPT
# 导入 PR diff 切块工具（把大 diff 切成可喂给模型的小块）
from app.services.rag.chunking import chunk_pr_files


# 模块说明（给零基础同事看的）：
# PRReviewAgent 是「GitHub Pull Request 审查专家」。
# 它接收一个 PR（改了哪些文件、diff 内容、review 评论、关联 CI 状态），自动判断：
# 风险点、哪些文件敏感、有没有测试、要不要阻塞合并、最终合入建议，以及一条 review checklist。
# 输入：PR 数据（files/comments/state 等）+ 上下文（CI 摘要、代码线索、文档等）。
# 输出：一份 PRReviewOutput（含 merge_recommendation、review_findings、risk_points、blocking_issues、test_suggestions 等）。
# 被谁调用：WorkflowOrchestrator 派发 pr_review 任务时调用；或 ChatAgent 的 pr_review_agent.analyze_pr 工具。
# 典型调用链：GitHub 同步 PR → 切块 diff → PRReviewAgent.run() → 本地规则兜底 + LLM 增强 → 合并输出。
# 为什么这样写：同样采用「本地规则兜底 + LLM 增强」双保险；本地规则能立刻识别「敏感文件/缺测试/失败 CI」等硬风险，
#               不依赖 LLM 也能给出靠谱的合入建议。
class PRReviewAgent(BaseAgent):
    """PRReviewAgent."""
    name = "pr_review_agent"
    description = "总结 PR diff，并生成风险点、检查清单和测试建议。"
    available_tools = ["get_pull_request_detail", "get_pull_request_files", "chunk_diff"]

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    # 主入口：先切块 diff，再算本地兜底，再尝试 LLM 增强，最后合并返回
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 把 PR 文件列表切块（避免一次性把巨大 diff 喂给模型）
        diff_chunks = chunk_pr_files(input_data.get("files", []))
        fallback = self._fallback_review(input_data, context, diff_chunks)
        llm_output = await self.llm.chat_json(PR_REVIEW_PROMPT, {"pull_request": input_data, "diff_chunks": diff_chunks, "context": context})
        if llm_output:
            try:
                return PRReviewOutput.model_validate(self._merge_with_fallback(llm_output, fallback)).model_dump()
            except Exception:
                pass  # LLM 输出不合法就退回兜底
        return fallback

    # 本地规则兜底审查：基于文件名关键词、CI 状态、评论数量、diff 体量等判断风险
    def _fallback_review(self, input_data: dict[str, Any], context: dict[str, Any], diff_chunks: list[dict[str, Any]]) -> dict[str, Any]:
        files = input_data.get("files", [])
        comments = input_data.get("comments", [])
        # 取出所有文件名
        filenames = [item.get("filename", "") for item in files]
        # 统计增删行数
        total_additions = sum(int(item.get("additions") or 0) for item in files)
        total_deletions = sum(int(item.get("deletions") or 0) for item in files)
        # 敏感文件：文件名含 auth/security/migration/schema/payment 等关键词
        risky_files = [
            name
            for name in filenames
            if any(token in name.lower() for token in ["auth", "security", "migration", "schema", "payment", "permission", "config", "ci", "workflow"])
        ]
        # 测试文件：文件名含 test/spec/__tests__/pytest/playwright 等
        test_files = [
            name
            for name in filenames
            if any(token in name.lower() for token in ["test", "spec", "__tests__", "pytest", "playwright"])
        ]
        # 计算 patch 总字符数（用于判断 diff 是否过大）
        total_patch_chars = sum(len(item.get("patch") or "") for item in files)
        risk_points = []       # 风险点列表
        blocking_issues = []   # 阻塞问题列表
        review_findings = []   # 审查发现（带 P1/P2/P3 级别）
        ci_summary = context.get("ci_summary") or {}
        failed_ci = int(ci_summary.get("failed_runs") or 0)  # 关联失败 CI 次数
        # 有失败 CI → 阻塞 + 一条 P1 发现
        if failed_ci:
            message = f"关联仓库仍有 {failed_ci} 个失败 CI，需要先确认是否由当前 PR 引入或阻塞合并。"
            blocking_issues.append(message)
            review_findings.append(self._finding("P1", "失败 CI 阻塞合并", message, "先修复或解释失败 CI，再重新评估合入。"))
        # 有敏感文件 → 风险点 + 一条 P2 发现
        if risky_files:
            message = f"高敏感文件需要重点审查：{', '.join(risky_files[:5])}"
            risk_points.append(message)
            review_findings.append(self._finding("P2", "敏感文件变更需要重点验证", message, "补充代码审查记录、回归测试或人工验证说明。"))
        # 有未处理 review 评论 → 风险点 + 阻塞 + 一条 P2 发现
        if comments:
            message = f"已有 {len(comments)} 条 review comment，需要确认是否都已处理。"
            risk_points.append(message)
            blocking_issues.append(message)
            review_findings.append(self._finding("P2", "Review 评论尚需确认", message, "逐条回应并确认评论已解决后再放行。"))
        # diff 过大 → 风险点 + 一条 P3 发现（非阻塞）
        if total_patch_chars > 12000 or len(diff_chunks) > 8:
            message = "Diff 较大，建议按模块分块 Review，避免遗漏跨文件影响。"
            risk_points.append(message)
            review_findings.append(self._finding("P3", "Diff 体量较大", message, "按模块拆分审查路径，必要时补充分阶段验证。", blocking=False))
        # 没测试文件且（有敏感文件或大改动）→ 阻塞 + 一条 P2 发现
        if files and not test_files and (risky_files or total_additions + total_deletions > 250):
            message = "当前变更缺少明显测试文件，合并前需要补充自动化测试或明确手工验证记录。"
            blocking_issues.append(message)
            review_findings.append(self._finding("P2", "缺少测试覆盖证明", message, "补充自动化测试，或留下可复核的手工验证记录。"))
        # 兜底：如果上面都没产生风险点，给一条通用提示
        if not risk_points:
            risk_points.append("需要确认边界条件和异常路径是否有测试覆盖")
        # 兜底：如果上面都没产生发现，给一条 P3 无阻断项发现
        if not review_findings:
            review_findings.append(
                self._finding(
                    "P3",
                    "未发现明确阻断项",
                    "基于当前 diff、评论和 CI 摘要，未发现明确 P1/P2 阻断项。",
                    "按 checklist 完成最终人工确认。",
                    blocking=False,
                )
            )

        # 综合判断合入建议（拒绝/暂缓/修改后合入/建议合入）
        merge_recommendation, recommendation_reason = self._choose_recommendation(input_data, files, risk_points, blocking_issues, failed_ci)
        # 计划步骤（描述审查思路，给 LLM/展示用）
        plan = [
            "理解 PR 标题、描述、分支、变更文件和关联上下文。",
            "按模块拆分 diff 风险，识别敏感路径和大体量改动。",
            "读取相关源代码、项目文档和历史会话证据。",
            "检查 CI 状态、失败记录和现有 review comments。",
            "形成合入建议、阻塞项、测试建议和 review checklist。",
        ]
        # 已执行步骤（给一份「我实际看了什么」的回执）
        executed_steps = [
            f"已读取 PR #{input_data.get('number')}，状态 {input_data.get('state') or 'unknown'}，作者 {input_data.get('author') or 'unknown'}。",
            f"已扫描 {len(files)} 个变更文件，合计 +{total_additions} / -{total_deletions}。",
            f"已识别 {len(risky_files)} 个敏感文件、{len(test_files)} 个测试相关文件、{len(comments)} 条 review comment。",
            f"已纳入 {len(context.get('code_references') or [])} 条代码线索、{len(context.get('project_documents') or [])} 条项目文档、{failed_ci} 个失败 CI 信号。",
            f"已输出结论：{merge_recommendation}。",
        ]
        # 组装 PRReviewOutput 并转字典
        return PRReviewOutput(
            summary=f"PR #{input_data.get('number')} 修改了 {len(files)} 个文件，主要集中在 {', '.join(filenames[:3]) or '未提供文件'}。",
            plan=plan,
            executed_steps=executed_steps,
            merge_recommendation=merge_recommendation,
            recommendation_reason=recommendation_reason,
            review_findings=review_findings,
            key_changes=[f"更新 {name}" for name in filenames[:5]],
            risk_points=risk_points if files else ["缺少 changed files，风险判断有限"],
            blocking_issues=blocking_issues,
            review_checklist=["确认接口兼容性", "确认错误处理路径", "确认测试覆盖关键分支", "确认 review comments 已回应"],
            test_suggestions=self._test_suggestions(filenames, risky_files, failed_ci),
            files_need_attention=(risky_files or filenames)[:5],
            review_comments=[str(comment.get("body") or "").strip() for comment in comments[:5] if str(comment.get("body") or "").strip()],
            confidence=self._estimate_confidence(files, context, failed_ci, blocking_issues),
        ).model_dump()

    # 把 LLM 输出与兜底合并：LLM 优先，但兜底补缺失项，并对发现/阻塞项做规范化
    def _merge_with_fallback(self, output: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        # LLM 覆盖兜底
        merged = {**fallback, **output}
        # 这些列表类字段，如果 LLM 给的是空，就用兜底
        for key in [
            "plan",
            "executed_steps",
            "review_findings",
            "key_changes",
            "risk_points",
            "blocking_issues",
            "review_checklist",
            "test_suggestions",
            "files_need_attention",
            "review_comments",
        ]:
            if not merged.get(key):
                merged[key] = fallback[key]
        # 字符串类字段同理
        for key in ["merge_recommendation", "recommendation_reason", "summary"]:
            if not str(merged.get(key) or "").strip():
                merged[key] = fallback[key]
        # 规范化审查发现，并从发现里重新推导阻塞项
        merged["review_findings"] = self._normalize_review_findings(merged.get("review_findings"), fallback)
        merged["blocking_issues"] = self._blocking_issues_from_findings(merged.get("blocking_issues"), merged["review_findings"])
        # 重要规则：如果有阻塞项，但 LLM 给了「建议合入」，强制降级为「修改后合入」（绝不带着阻塞项放行）
        if merged["blocking_issues"] and merged.get("merge_recommendation") == "建议合入":
            merged["merge_recommendation"] = "修改后合入"
            merged["recommendation_reason"] = "存在 P1/P2 审查发现，完成修复或确认后才能放行。"
        return merged

    # 构造一条审查发现（finding）：默认 P1/P2 视为阻塞，否则按传入 blocking 决定
    def _finding(
        self,
        severity: str,
        title: str,
        evidence: str,
        required_action: str,
        *,
        blocking: bool | None = None,
    ) -> dict[str, Any]:
        return {
            "severity": severity,
            "title": title,
            "evidence": evidence,
            "required_action": required_action,
            # 如果没显式指定 blocking，就用「是否 P1/P2」来决定是否阻塞
            "blocking": severity in {"P1", "P2"} if blocking is None else blocking,
        }

    # 规范化审查发现列表：过滤非法项，统一 severity 到 P1/P2/P3，补齐标题/证据/动作/阻塞标志
    def _normalize_review_findings(self, items: Any, fallback: dict[str, Any]) -> list[dict[str, Any]]:
        source = items if isinstance(items, list) and items else fallback.get("review_findings") or []
        normalized: list[dict[str, Any]] = []
        for item in source:
            if not isinstance(item, dict):
                continue
            severity = str(item.get("severity") or "").strip().upper()
            if severity not in {"P1", "P2", "P3"}:
                severity = "P3"  # 不认识的分级一律当 P3
            title = str(item.get("title") or item.get("summary") or item.get("message") or "").strip()
            evidence = str(item.get("evidence") or item.get("reason") or item.get("description") or "").strip()
            required_action = str(item.get("required_action") or item.get("action") or item.get("recommendation") or "").strip()
            if not title and not evidence:
                continue  # 既没有标题也没有证据，丢弃
            raw_blocking = item.get("blocking")
            blocking = severity in {"P1", "P2"} if raw_blocking is None else raw_blocking is True
            normalized.append(
                self._finding(
                    severity,
                    title or evidence[:80],  # 标题缺失时用证据前 80 字顶上
                    evidence or title,
                    required_action or "请补充可复核的处理说明。",
                    blocking=blocking,
                )
            )
        return normalized or fallback.get("review_findings") or []

    # 从「显式 blocking_issues + 审查发现里的 P1/P2/阻塞项」合并去重出最终阻塞项列表
    def _blocking_issues_from_findings(self, blocking_issues: Any, review_findings: list[dict[str, Any]]) -> list[str]:
        rows = [str(item).strip() for item in blocking_issues or [] if str(item).strip()]
        for finding in review_findings:
            if finding.get("severity") in {"P1", "P2"} or finding.get("blocking"):
                action = str(finding.get("required_action") or "").strip()
                title = str(finding.get("title") or "").strip()
                message = f"{title}：{action}" if title and action else title or action
                if message:
                    rows.append(message)
        # 去重并保持顺序
        return list(dict.fromkeys(rows))

    # 综合判断合入建议：拒绝 / 暂缓 / 修改后合入 / 建议合入
    def _choose_recommendation(
        self,
        input_data: dict[str, Any],
        files: list[dict[str, Any]],
        risk_points: list[str],
        blocking_issues: list[str],
        failed_ci: int,
    ) -> tuple[str, str]:
        state = str(input_data.get("state") or "").lower()
        text = f"{input_data.get('title', '')} {input_data.get('body', '')}".lower()
        # 已关闭且未合并 → 拒绝
        if state == "closed" and not input_data.get("merged_at"):
            return "拒绝", "PR 已关闭且没有合并记录，当前不建议继续合入。"
        # 描述含 WIP/draft/暂缓 等词 → 暂缓
        if any(token in text for token in ["do not merge", "wip", "draft", "blocked", "暂缓", "不要合并"]):
            return "暂缓", "PR 描述中存在 WIP/blocked/暂缓信号，需要等待作者更新。"
        # 有失败 CI → 暂缓
        if failed_ci:
            return "暂缓", "存在失败 CI 信号，合并前需要先确认根因并恢复质量门禁。"
        # 没有变更文件 → 暂缓（无法判断影响）
        if not files:
            return "暂缓", "缺少 changed files，无法判断真实影响范围。"
        # 有阻塞项 → 修改后合入
        if blocking_issues:
            return "修改后合入", "存在可修复的阻塞项，完成测试或说明补充后可重新评估。"
        # 有明显风险点（高敏感/较大/review comment）→ 修改后合入
        if risk_points and any(token in " ".join(risk_points) for token in ["高敏感", "较大", "review comment"]):
            return "修改后合入", "PR 有明确风险点，建议先完成重点 review 和验证再合入。"
        # 默认：没明显阻塞 → 建议合入
        return "建议合入", "未发现明显阻塞项，按 checklist 完成最终确认后可以合入。"

    # 生成测试建议：根据改动涉及前端/后端/敏感文件/失败 CI，给出针对性建议
    def _test_suggestions(self, filenames: list[str], risky_files: list[str], failed_ci: int) -> list[str]:
        suggestions = ["运行相关单元测试和集成测试"]
        lowered = " ".join(filenames).lower()
        if any(token in lowered for token in ["frontend", ".tsx", ".ts", "next", "react"]):
            suggestions.append("运行前端类型检查/构建，并覆盖关键页面交互")
        if any(token in lowered for token in ["backend", ".py", "api", "db"]):
            suggestions.append("运行后端 pytest，并覆盖接口或数据库边界")
        if risky_files:
            suggestions.append("对敏感文件补充回归测试或人工验证记录")
        if failed_ci:
            suggestions.append("先修复失败 CI，再重新运行完整流水线")
        return list(dict.fromkeys(suggestions))  # 去重

    # 估算置信度：基于是否有文件、代码/文档/会话证据、失败 CI、阻塞项累加得分，夹在 [0.38, 0.88]
    def _estimate_confidence(self, files: list[dict[str, Any]], context: dict[str, Any], failed_ci: int, blocking_issues: list[str]) -> float:
        score = 0.48
        if files:
            score += 0.12
        if context.get("code_references"):
            score += 0.08
        if context.get("project_documents"):
            score += 0.06
        if context.get("conversation_evidence"):
            score += 0.05
        if failed_ci:
            score += 0.04
        if blocking_issues:
            score += 0.04
        return round(max(0.38, min(0.88, score)), 2)
