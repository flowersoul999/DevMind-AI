"""DevMind AI module."""

# 允许类型注解里直接使用类本身，兼容老版本 Python 注解写法
from __future__ import annotations

# re：正则表达式库，用于文本归一化
import re
# datetime：处理时间戳，用于按时间排序
from datetime import datetime
# Any：任意类型占位
from typing import Any

# 导入 Agent 基类
from app.services.agents.base import BaseAgent


# 模块说明（给零基础同事看的）：
# RecommendationAgent 是「聊天提问推荐器」。
# 它根据当前仓库的 Issue / PR / CI 状态，推荐几个「用户最可能想问的上下文相关提问」，
# 例如「定位 CI 失败原因」「这个 PR 有啥风险」「这个 Issue 分给谁」。
# 输入：issues / pull_requests / workflow_runs 列表 + 想要的数量 limit + 想排除的问题 exclude。
# 输出：一个字典，含 suggestions（推荐提问列表）和 source_counts（各类数据条数）。
# 被谁调用：前端「智能提问」气泡、ChatAgent 初始化时生成引导问题等。
# 典型调用链：前端加载仓库 → 调用 RecommendationAgent.run() → 拿到推荐提问展示给用户点选。
# 为什么这样写：根据「仓库当前有哪些信号」动态生成推荐，比写死的固定问题更贴场景；
#               同时做去重、限制数量、排除已选项，保证推荐可控不刷屏。
class RecommendationAgent(BaseAgent):
    """RecommendationAgent."""
    name = "recommendation_agent"
    description = "根据当前仓库的 Issue、PR 和 CI 状态推荐上下文相关的聊天提问。"
    available_tools = ["repository_snapshot"]

    # 主入口：生成去重后的推荐提问列表
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 数量限制：夹在 [4,5]（最少 4 个，最多 5 个）
        limit = min(max(int(input_data.get("limit") or 5), 4), 5)
        # 需要排除的提问（已展示过/用户不想看的）归一化集合
        excluded = {self._normalize(item) for item in input_data.get("exclude") or []}
        # 生成候选问题池
        candidates = self._build_candidates(input_data)

        selected: list[str] = []  # 最终选中的问题
        seen = set(excluded)      # 已见过的归一化文本（含排除项），用于去重
        # 第一轮：从候选里挑，跳过已排除/重复的，直到达到 limit
        for prompt in candidates:
            normalized = self._normalize(prompt)
            if not normalized or normalized in seen:
                continue
            selected.append(prompt)
            seen.add(normalized)
            if len(selected) >= limit:
                break

        # 如果第一轮没凑够（候选太少且都排除了），再用候选兜底补满（这次不再排除）
        if not selected and excluded:
            for prompt in candidates:
                normalized = self._normalize(prompt)
                if not normalized:
                    continue
                selected.append(prompt)
                if len(selected) >= limit:
                    break

        # 返回推荐结果 + 各类数据条数统计
        return {
            "agent": self.name,
            "suggestions": selected,
            "source_counts": {
                "issues": len(input_data.get("issues") or []),
                "pull_requests": len(input_data.get("pull_requests") or []),
                "workflow_runs": len(input_data.get("workflow_runs") or []),
            },
        }

    # 构造候选问题池：根据仓库实际有哪些数据（失败 CI / open PR / open Issue）生成针对性问题
    def _build_candidates(self, input_data: dict[str, Any]) -> list[str]:
        # 按时间倒序排（最新的排前面）
        issues = sorted(input_data.get("issues") or [], key=self._sort_key, reverse=True)
        prs = sorted(input_data.get("pull_requests") or [], key=self._sort_key, reverse=True)
        runs = sorted(input_data.get("workflow_runs") or [], key=self._sort_key, reverse=True)
        # 如果三类数据都没有 → 返回一组「仓库初始化」通用引导问题
        if not issues and not prs and not runs:
            return [
                "概览这个新仓库的当前状态",
                "查看这个项目的代码结构",
                "我该如何同步这个仓库的 Issue、PR 和 CI",
                "帮我建立项目记忆和团队分工",
                "生成初始化项目检查清单",
                "这个仓库接下来应该先配置什么",
                "帮我规划第一次项目同步和分析",
            ]

        # 筛选 open 状态的 Issue / PR，以及失败状态的 CI
        open_issues = [item for item in issues if str(item.get("state") or "").lower() == "open"]
        open_prs = [item for item in prs if str(item.get("state") or "").lower() == "open"]
        failed_runs = [item for item in runs if str(item.get("conclusion") or "").lower() == "failure"]

        candidates: list[str] = []

        # 针对每个失败 CI（最多 5 个），生成排查类问题
        for run in failed_runs[:5]:
            name = self._short(run.get("name") or "最近 CI")
            candidates.extend(
                [
                    f"定位 {name} 失败原因",
                    f"把 {name} 的失败日志整理成修复步骤",
                    f"分析 {name} 失败是否和最近 PR 有关",
                    f"给 {name} 生成排查 checklist",
                ]
            )

        # 没有失败 CI 但有 CI 运行 → 生成「CI 健康」类问题
        if not failed_runs and runs:
            latest = self._short(runs[0].get("name") or "最近 CI")
            candidates.extend(
                [
                    f"检查 {latest} 的 CI 状态",
                    "最近 CI 有没有潜在风险",
                    "总结最近 workflow 的健康状况",
                    "如果 CI 失败，下一步应该先看哪里",
                ]
            )

        # 针对每个 open PR（最多 5 个），生成风险/测试/合并前类问题
        for pr in open_prs[:5]:
            number = pr.get("number")
            label = f"PR #{number}" if number else "最新 PR"
            title = self._short(pr.get("title") or "")
            suffix = f"：{title}" if title else ""
            candidates.extend(
                [
                    f"总结 {label}{suffix} 的风险",
                    f"检查 {label} 需要补哪些测试",
                    f"给 {label} 生成合并前 checklist",
                    f"{label} 是否会影响 CI 或发布",
                ]
            )

        # 针对每个 open Issue（最多 5 个），生成负责人/优先级/澄清/拆分类问题
        for issue in open_issues[:5]:
            number = issue.get("number")
            label = f"Issue #{number}" if number else "最新 Issue"
            title = self._short(issue.get("title") or "")
            suffix = f"：{title}" if title else ""
            candidates.extend(
                [
                    f"{label}{suffix} 应该分给谁",
                    f"分析 {label} 的优先级和复杂度",
                    f"给 {label} 写一条澄清评论草稿",
                    f"把 {label} 拆成可执行任务",
                ]
            )

        # 有失败 CI 时，补两条跨 CI 的全局问题
        if failed_runs:
            candidates.extend(
                [
                    "最近 CI 为什么失败",
                    "把失败 CI 和相关 PR 串起来分析" if prs else "把失败 CI 整理成排查步骤",
                ]
            )
        # 有 PR 时，补一组 PR 全局问题
        if prs:
            candidates.extend(
                [
                    "总结这个 PR 的风险",
                    "按风险排序当前打开的 PR",
                    "哪些 PR 适合今天合并",
                    "生成一份合并前风险检查清单",
                ]
            )
        # 有 Issue 时，补一组 Issue 全局问题
        if issues:
            candidates.extend(
                [
                    "这个 Issue 应该分给谁",
                    "按优先级整理当前打开的 Issue",
                    "哪些 Issue 需要补充复现信息",
                    "根据团队成员能力推荐 Issue 负责人",
                    "分析打开时间最久的问题",
                ]
            )
        # Issue 和 PR 都有 → 补站会摘要类问题
        if issues and prs:
            candidates.extend(["把当前 Issue 和 PR 做成站会摘要", "找出最可能拖慢交付的事项"])

        # 最后补一大组「通用」问题（无论仓库状态如何都可能有用）
        candidates.extend(
            [
                "生成本周研发周报",
                "概览当前仓库状态",
                "找出最值得优先处理的 3 件事",
                "生成一份今日工程状态摘要",
                "梳理当前项目的主要阻塞点",
                "看一下这个项目的代码结构",
                "搜索当前代码里的测试配置",
                "检查最近变更可能带来的风险",
                "帮我制定下一步排查计划",
                "哪些任务需要先找负责人确认",
                "生成一份发给团队的进展说明",
                "检查是否有需要安全确认的写操作",
                "当前仓库有哪些知识库内容可用",
                "本周研发周报应该包含哪些重点",
                "总结当前项目的质量风险",
                "列出今天可以快速推进的事项",
            ]
        )
        # 整体去重后返回
        return self._dedupe(candidates)

    # 去重：按归一化文本去重，保持原顺序
    def _dedupe(self, values: list[str]) -> list[str]:
        result: list[str] = []
        seen: set[str] = set()
        for value in values:
            normalized = self._normalize(value)
            if not normalized or normalized in seen:
                continue
            result.append(value)
            seen.add(normalized)
        return result

    # 归一化：去首尾空白、压扁内部空白、转小写，用于比较「是不是同一个问题」
    def _normalize(self, value: Any) -> str:
        return re.sub(r"\s+", " ", str(value or "").strip().lower())

    # 截断文本：超过 max_length 就截到 max_length 并加省略号（用于问题里嵌的标题/CI 名）
    def _short(self, value: Any, max_length: int = 22) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if len(text) <= max_length:
            return text
        return text[:max_length].rstrip() + "..."

    # 排序键：从 updated_at/created_at/merged_at/closed_at 里取第一个有效时间戳（用于倒序排最新的在前）
    def _sort_key(self, item: dict[str, Any]) -> float:
        for key in ["updated_at", "created_at", "merged_at", "closed_at"]:
            value = item.get(key)
            if isinstance(value, datetime):
                return value.timestamp()
            if isinstance(value, str) and value:
                try:
                    # 把 ISO 时间字符串（可能带 Z）转成带时区的时间戳
                    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
                except ValueError:
                    continue
        return 0.0
