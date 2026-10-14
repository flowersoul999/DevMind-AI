"""
reports.py —— 周报（Weekly Report）接口的数据契约（Schema 层）

本文件服务于「生成周期研发周报」接口：
- WeeklyReportRequest：给定仓库与时间范围（起止日期），请求生成周报；
- WeeklyReportResponse：返回一份 Markdown 格式的研发周报。

date 只含年月日（不含时分秒），适合「2025-01-01 ~ 2025-01-07」这种区间。."""

from datetime import date  # date：仅日期（年-月-日），不含时间
from uuid import UUID  # UUID：唯一 ID

from pydantic import BaseModel  # BaseModel：模型基类


class WeeklyReportRequest(BaseModel):
    """WeeklyReportRequest."""
    # 生成周报的请求体
    repo_id: UUID  # 仓库 ID，必填
    start_date: date  # 统计开始日期，必填
    end_date: date  # 统计结束日期，必填


class WeeklyReportResponse(BaseModel):
    """WeeklyReportResponse."""
    # 周报响应：直接返回 Markdown 文本，前端拿到后渲染成报告
    report_markdown: str  # 周报内容（Markdown 格式），必填
