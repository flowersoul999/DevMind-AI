"""DevMind AI module."""

# 正则表达式库，用于从日志里提取文件名
import re
# Any：任意类型占位
from typing import Any

# 导入分析输出模型：CI 调试结果
from app.schemas.analysis import CIDebugOutput
# 导入 Agent 基类
from app.services.agents.base import BaseAgent
# 导入大模型客户端
from app.services.llm.client import LLMClient
# 导入 CI 调试提示词模板
from app.services.llm.prompts import CI_DEBUG_PROMPT


# 模块说明（给零基础同事看的）：
# CIDebugAgent 是「GitHub Actions / CI 失败排查专家」。
# 它接收一次失败的 workflow run（日志文本、jobs、steps），自动判断：
# 失败类型（依赖/测试/构建/权限/环境变量/未知）、首个关键错误、根因推测、修复步骤，以及「是否阻塞合并」。
# 输入：workflow run 数据（logs_text/jobs/conclusion 等）+ 上下文（最近 PR、代码线索等）。
# 输出：一份 CIDebugOutput（含 failure_type、first_error、root_cause、fix_steps、is_merge_blocking 等）。
# 被谁调用：WorkflowOrchestrator 派发 ci_debug 任务时调用；或 ChatAgent 的 ci_debug_agent.analyze_workflow_run 工具。
# 典型调用链：GitHub 同步失败 CI → CIDebugAgent.run() → 本地规则解析日志 + LLM 增强 → 合并输出。
# 为什么这样写：CI 日志是纯文本，本地就能用关键词快速归类失败类型、抓出首个错误块，
#               这部分不依赖 LLM 也很准；LLM 只在「综合表述」上增强，保证离线也可用。
class CIDebugAgent(BaseAgent):
    """CIDebugAgent."""
    name = "ci_debug_agent"
    description = "分析 workflow 失败日志，并提出排查步骤。"
    available_tools = ["get_workflow_run", "get_workflow_logs", "extract_failed_log_sections"]

    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or LLMClient()

    # 主入口：先算本地兜底，再尝试 LLM 增强，最后合并返回
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        fallback = self._fallback_debug(input_data, context)
        llm_output = await self.llm.chat_json(CI_DEBUG_PROMPT, {"workflow_run": input_data, "context": context})
        if llm_output:
            try:
                return CIDebugOutput.model_validate(self._merge_with_fallback(llm_output, fallback)).model_dump()
            except Exception:
                pass
        return fallback

    # 本地规则兜底：解析日志、jobs/steps，推断失败类型、根因、是否阻塞合并
    def _fallback_debug(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        logs = (input_data.get("logs_text") or "").lower()  # 日志小写，便于关键词匹配
        raw_logs = str(input_data.get("logs_text") or "")    # 原始日志，用于抓错误块
        # 找出所有结论为 failure 的 job
        failed_jobs = [
            job
            for job in input_data.get("jobs", [])
            if isinstance(job, dict) and job.get("conclusion") == "failure"
        ]
        # 找出失败 job 里结论为 failure 的 step 名字
        failed_steps = [
            step.get("name")
            for job in failed_jobs
            for step in job.get("steps", [])
            if isinstance(step, dict) and step.get("conclusion") == "failure" and step.get("name")
        ]
        # 分类失败类型（依赖/测试/构建/权限/环境变量/未知）
        failure_type = self._classify_failure(logs)
        # 提取首个关键错误块
        first_error = self._extract_first_error(raw_logs)
        # 从日志里提取涉及的源文件
        related_files = self._extract_related_files(raw_logs, context)
        failed_job_names = [str(job.get("name")) for job in failed_jobs if job.get("name")]
        # 拼一些人类可读的提示
        job_hint = f"失败 job：{', '.join(failed_job_names[:3])}。" if failed_job_names else ""
        step_hint = f"失败 step：{', '.join(failed_steps[:3])}。" if failed_steps else ""
        # 推断根因描述
        root_cause = self._infer_root_cause(failure_type, first_error, failed_job_names, failed_steps)
        # 生成修复步骤
        fix_steps = self._fix_steps(failure_type, related_files)
        # 判断是否阻塞合并
        is_merge_blocking = self._is_merge_blocking(input_data, failure_type)
        blocking_reason = (
            "当前 workflow run 结论为 failure，属于合并前需要恢复的质量门禁。"
            if is_merge_blocking
            else "当前 workflow run 没有失败结论，不阻塞合并。"
        )
        plan = [
            "找出失败 job 和失败 step。",
            "提取首个关键错误块。",
            "判断失败类型：依赖、测试、构建、权限、环境变量或未知。",
            "搜索相关代码、测试文件和 workflow 配置。",
            "关联最近 PR 或变更模块。",
            "输出根因、修复步骤和是否阻塞合并。",
        ]
        executed_steps = [
            f"已识别失败 job {len(failed_jobs)} 个：{', '.join(failed_job_names[:3]) or '未提供 job 明细'}。",
            f"已识别失败 step {len(failed_steps)} 个：{', '.join(failed_steps[:3]) or '未提供 step 明细'}。",
            f"已提取首个关键错误块：{first_error or '日志中未找到明确 error 块'}。",
            f"已分类失败类型为 {failure_type}，关联文件 {len(related_files)} 个。",
            f"已结合最近 PR 信号 {len(context.get('recent_prs') or [])} 条和代码/配置线索 {len(context.get('code_references') or [])} 条。",
            "已生成根因、修复步骤和阻塞判断。",
        ]
        # 组装 CIDebugOutput 并转字典
        return CIDebugOutput(
            failure_summary=input_data.get("name", "CI run") + f" 执行失败。{job_hint}{step_hint}需要结合日志定位首个错误块。",
            failure_type=failure_type,
            plan=plan,
            executed_steps=executed_steps,
            first_error=first_error,
            root_cause=root_cause,
            possible_causes=self._possible_causes(failure_type),
            fix_steps=fix_steps,
            debug_steps=["查看失败 job 的首个错误块", "本地复现对应命令", "检查最近 PR 修改的相关文件", "修复后重新运行失败 job 和完整 workflow"],
            related_files=related_files,
            is_merge_blocking=is_merge_blocking,
            blocking_reason=blocking_reason,
            confidence=self._estimate_confidence(first_error, failed_jobs, related_files, context),
        ).model_dump()

    # 把 LLM 输出与兜底合并：LLM 优先，但兜底补缺；特别地，失败类型若 LLM 给 unknown 而兜底不是，就用兜底
    def _merge_with_fallback(self, output: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        merged = {**fallback, **output}
        for key in ["plan", "executed_steps", "possible_causes", "fix_steps", "debug_steps", "related_files"]:
            if not merged.get(key):
                merged[key] = fallback[key]
        for key in ["failure_summary", "failure_type", "root_cause", "blocking_reason"]:
            if not str(merged.get(key) or "").strip():
                merged[key] = fallback[key]
        # LLM 没认出类型但兜底认出来了 → 用兜底（更靠谱）
        if merged.get("failure_type") == "unknown" and fallback.get("failure_type") != "unknown":
            merged["failure_type"] = fallback["failure_type"]
        # first_error 缺失用兜底
        if not merged.get("first_error"):
            merged["first_error"] = fallback["first_error"]
        return merged

    # 分类失败类型：按优先级匹配关键词（权限 > 环境变量 > 依赖 > lint > 构建 > 测试），匹配不到标 unknown
    def _classify_failure(self, logs: str) -> str:
        ordered_rules = [
            ("permission", ["permission", "denied", "forbidden", "eacces", "权限"]),
            ("environment", ["missing env", "environment variable", "secret", "not set", "环境变量"]),
            ("dependency", ["npm err", "pip install", "dependency", "module not found", "no matching distribution", "cannot find module", "依赖"]),
            ("lint", ["eslint", "lint", "ruff", "flake8", "biome"]),
            ("build", ["build failed", "compilation", "webpack", "vite", "next build", "tsc", "type error", "构建"]),
            ("test", ["pytest", "assert", "test failed", "tests failed", "failed tests", "断言", "测试"]),
        ]
        for failure_type, tokens in ordered_rules:
            if any(token in logs for token in tokens):
                return failure_type
        # 日志里至少提到了失败/错误，但无法归类
        if "failed" in logs or "error" in logs:
            return "unknown"
        return "unknown"

    # 提取首个关键错误块：扫描日志，命中 error/failed/traceback 等关键词就取前后若干行，截到 900 字
    def _extract_first_error(self, logs: str) -> str | None:
        if not logs.strip():
            return None
        lines = [line.strip() for line in logs.splitlines() if line.strip()]
        for index, line in enumerate(lines):
            lowered = line.lower()
            if any(token in lowered for token in ["error", "failed", "failure", "exception", "traceback", "assertionerror", "npm err", "fatal"]):
                # 取命中行前 2 行、后 5 行作为上下文块
                start = max(0, index - 2)
                end = min(len(lines), index + 5)
                return "\n".join(lines[start:end])[:900]
        # 找不到明确错误就返回前 6 行
        return "\n".join(lines[:6])[:900]

    # 从日志里正则提取文件名（py/ts/js/json/yml 等），并结合代码线索补充
    def _extract_related_files(self, logs: str, context: dict[str, Any]) -> list[str]:
        candidates = re.findall(r"[\w./\\-]+\.(?:py|ts|tsx|js|jsx|json|ya?ml|toml|ini|cfg|md)", logs)
        for item in context.get("code_references") or []:
            if isinstance(item, dict):
                title = str(item.get("title") or item.get("reference") or "").strip()
                if "." in title:
                    candidates.append(title)
        normalized = []
        for item in candidates:
            # 去掉包裹符号、反斜杠转正斜杠
            cleaned = item.strip("`'\"()[]{}:,;").replace("\\", "/")
            if cleaned and cleaned not in normalized:
                normalized.append(cleaned)
        return normalized[:8]  # 最多 8 个

    # 根据失败类型生成一句根因描述（结合失败 job/step 名称让描述更具体）
    def _infer_root_cause(self, failure_type: str, first_error: str | None, failed_job_names: list[str], failed_steps: list[str]) -> str:
        # 把失败 job/step 名称拼成一个 scope 描述
        scope = ", ".join(filter(None, [", ".join(failed_job_names[:2]), ", ".join(failed_steps[:2])]))
        if failure_type == "test":
            return f"失败集中在测试执行阶段{f'（{scope}）' if scope else ''}，首个错误指向断言、用例或被测行为不一致。"
        if failure_type == "build":
            return f"失败集中在构建/类型检查阶段{f'（{scope}）' if scope else ''}，首个错误通常来自编译、类型或打包配置问题。"
        if failure_type == "dependency":
            return "日志显示依赖安装或模块解析失败，优先检查 lockfile、包版本和 CI 缓存。"
        if failure_type == "permission":
            return "日志显示权限不足或访问被拒绝，优先检查 token、文件权限和 workflow 权限配置。"
        if failure_type == "environment":
            return "日志显示环境变量或 secret 缺失，优先检查 workflow env 配置和仓库 secrets。"
        if failure_type == "lint":
            return "日志显示 lint/格式检查失败，优先定位对应文件的规则违规并本地运行 lint。"
        return f"日志缺少足够明确的分类信号，首个错误块是当前最可靠的排查入口：{first_error[:160] if first_error else '未提供日志'}"

    # 给出每种失败类型「可能的原因」列表（供展示）
    def _possible_causes(self, failure_type: str) -> list[str]:
        mapping = {
            "test": ["测试断言不一致", "最近变更改变了接口行为", "测试数据或环境初始化不完整"],
            "build": ["类型错误或编译错误", "构建配置与代码变更不匹配", "生成产物或路径引用缺失"],
            "lint": ["格式或静态规则违规", "新增代码未经过本地 lint", "配置升级导致规则变化"],
            "dependency": ["依赖版本变化", "lockfile 未同步", "CI 缓存或 registry 配置异常"],
            "permission": ["workflow 权限不足", "token scope 不足", "脚本访问受限目录"],
            "environment": ["环境变量缺失", "secret 未配置", "CI 与本地默认配置不一致"],
            "unknown": ["首个错误块信息不足", "上游命令吞掉了真实错误", "需要查看完整 job 日志"],
        }
        return mapping[failure_type]

    # 给出每种失败类型的「修复步骤」列表（结合相关文件更聚焦）
    def _fix_steps(self, failure_type: str, related_files: list[str]) -> list[str]:
        focus = f"，重点检查 {', '.join(related_files[:3])}" if related_files else ""
        mapping = {
            "test": [f"本地运行失败测试命令并复现{focus}", "对照最近 PR 修改确认预期行为", "修正代码或测试数据后补充回归用例"],
            "build": [f"本地运行构建/类型检查命令{focus}", "修复首个编译或类型错误", "确认生成路径、导入路径和配置文件同步更新"],
            "lint": [f"本地运行 lint/format 命令{focus}", "修复首个规则违规", "必要时调整规则并说明原因"],
            "dependency": ["重新安装依赖并确认 lockfile 更新", "检查 package/requirements 与 CI 版本是否一致", "清理 CI 缓存后重跑"],
            "permission": ["检查 workflow permissions 配置", "确认 token/secrets 的 scope", "避免脚本写入受限目录或使用受限 API"],
            "environment": ["补齐 workflow env 或 repository secrets", "为本地和 CI 提供一致默认配置", "在日志中避免泄露 secret 明文"],
            "unknown": ["展开完整 job 日志", "找到最早失败命令的 stderr", "必要时在 workflow 中增加诊断输出后重跑"],
        }
        return mapping[failure_type]

    # 判断是否阻塞合并：workflow 结论是 failure，或（已结束且失败类型非 unknown）都算阻塞
    def _is_merge_blocking(self, input_data: dict[str, Any], failure_type: str) -> bool:
        conclusion = str(input_data.get("conclusion") or "").lower()
        status = str(input_data.get("status") or "").lower()
        return conclusion == "failure" or (status == "completed" and failure_type != "unknown")

    # 估算置信度：基于是否有首个错误、失败 job、相关文件、最近 PR/代码线索累加得分，夹在 [0.36, 0.86]
    def _estimate_confidence(self, first_error: str | None, failed_jobs: list[dict[str, Any]], related_files: list[str], context: dict[str, Any]) -> float:
        score = 0.42
        if first_error:
            score += 0.16
        if failed_jobs:
            score += 0.12
        if related_files:
            score += 0.08
        if context.get("recent_prs"):
            score += 0.05
        if context.get("code_references"):
            score += 0.05
        return round(max(0.36, min(0.86, score)), 2)
