"""
services/feedback_notifications.py —— 把「用户点踩的负面反馈」推送到飞书
============================================================================

当用户在前端对 AI 的某条回答点「踩」并选择原因时，我们不只把反馈存库，还会顺手
发一条消息到飞书群（机器人 webhook），让开发/运营同学第一时间知道「哪条回答翻车了」。

核心函数 send_negative_feedback_notification：
  - 组装一段中文通报文本（项目、反馈人、原因、问题、回答、执行轨迹链接）
  - 通过飞书自定义机器人 webhook 发到群
  - 无论发成功还是失败，都**不影响**反馈本身已落库（通知是锦上添花，不能因为群机器人
    挂了就把用户反馈弄丢）

REASON_LABELS 是把英文原因码翻译成中文给用户看的对照表。."""

import logging  # 写日志
from typing import Any  # 任意类型

import httpx  # 异步 HTTP 客户端，用来调飞书 webhook

from app.core.config import settings  # 全局配置：飞书 webhook 地址、轨迹链接前缀
from app.db.models import AgentRun, ChatFeedback, ChatMessage, Repository, User  # 相关数据库表


logger = logging.getLogger(__name__)  # 本模块的 logger

# 原因码 → 中文标签。用户点踩时前端传的是英文码（如 "inaccurate"），这里转成可读中文
REASON_LABELS = {
    "inaccurate": "内容不准确",
    "not_relevant": "没有解决问题",
    "missing_context": "缺少关键上下文",
    "unreliable_citation": "引用或证据不可靠",
    "tool_error": "工具执行异常",
    "other": "其他",
}


def _clip(value: str | None, limit: int) -> str:
    # 把文本「压缩成单行 + 截断」，用于把长回答塞进通知里
    text = " ".join((value or "").split())  # split/join 去掉多余换行和连续空格，变成一行
    return text if len(text) <= limit else f"{text[:limit]}..."  # 超过 limit 就切到 limit 字符并加省略号


def _trace_url(run_id: Any) -> str:
    # 拼出「查看这次回答执行轨迹」的链接
    path = f"/api/chat/feedback/trace/{run_id}"  # 轨迹页的固定路径
    base_url = (settings.feedback_trace_base_url or "").strip().rstrip("/")  # 配置里可能给了站点域名前缀
    return f"{base_url}{path}" if base_url else path  # 有域名就拼全链，没有就给相对路径


async def send_negative_feedback_notification(
    *,
    feedback: ChatFeedback,  # 这条反馈记录（含原因、评论、run_id）
    repo: Repository,  # 关联的仓库（用来显示项目名）
    message: ChatMessage,  # 被点踩的那条消息
    run: AgentRun | None,  # 关联的一次 Agent 运行（可能为空）
    reporter: User | None,  # 反馈人（可能为空，演示环境无登录）
) -> tuple[str, str | None]:
    # 返回值：(状态, 错误信息)。状态可能是 "not_configured" / "failed" / "sent"
    webhook_url = (settings.feishu_feedback_webhook_url or "").strip()  # 从配置取飞书机器人地址
    if not webhook_url:
        # 没配 webhook 就直接返回「未配置」，不报错——这是可选功能
        return "not_configured", None

    run_id = feedback.run_id or "未关联"  # 取运行 id，没有就写「未关联」
    # 逐行拼装通报内容
    lines = [
        "DevMind AI 收到一条负反馈",  # 标题
        f"项目：{repo.full_name}",  # 仓库全名（owner/name）
        f"反馈人：{reporter.name if reporter else '内部用户'}",  # 没用户就显示内部用户
        f"trace_id：{run_id}",
        f"message_id：{feedback.assistant_message_id}",  # 被点踩消息的 id
        f"路由：{run.route if run else message.route or 'unknown'}",  # 走的是哪条分析链路
        f"原因：{REASON_LABELS.get(feedback.reason or '', '未填写')}",  # 中文原因，查不到就「未填写」
    ]
    if feedback.comment:
        # 如果用户写了补充说明，附上（截断到 300 字）
        lines.append(f"补充说明：{_clip(feedback.comment, 300)}")
    if run:
        # 如果关联到一次 Agent 运行，把当时的「用户问题」和「AI 回答」也贴出来（各截断）
        lines.extend(
            [
                f"用户问题：{_clip(run.user_message, 300)}",
                f"Agent 回答：{_clip(run.final_answer, 500)}",
            ]
        )
    # 三元表达式：有 run_id 就给轨迹链接，没有就说明「未关联」
    lines.append(f"执行轨迹：{_trace_url(run_id)}" if feedback.run_id else "执行轨迹：该历史回答未关联 run_id")

    try:
        # 真正发飞书：用自定义机器人 webhook，msg_type=text 表示文本消息
        async with httpx.AsyncClient(timeout=5.0) as client:  # 5 秒超时，通知别卡太久
            response = await client.post(
                webhook_url,
                json={"msg_type": "text", "content": {"text": "\n".join(lines)}},  # 多行文本用 \n 连接
            )
            response.raise_for_status()  # 4xx/5xx 抛异常
            payload = response.json()  # 飞书返回 {"StatusCode":0,...} 或 {"code":...}
            # 飞书成功时 StatusCode 是 0；兼容不同的返回字段名
            status_code = payload.get("StatusCode", payload.get("code", 0)) if isinstance(payload, dict) else 0
            if status_code not in {0, "0", None}:
                # 非 0 视为失败，抛出错误信息
                raise RuntimeError(str(payload.get("StatusMessage") or payload.get("msg") or payload))
    except Exception as exc:  # 为什么捕获所有异常：通知失败绝不能影响反馈已落库，只能记日志返回 failed
        logger.warning("发送飞书负反馈通知失败：%s", exc)
        return "failed", _clip(str(exc), 1000)  # 返回失败状态和错误信息（截断）
    return "sent", None  # 成功
