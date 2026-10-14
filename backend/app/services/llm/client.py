"""
services/llm/client.py —— 项目统一调用大模型的「话筒」
============================================================================

这是整个项目和大语言模型（LLM，比如 DeepSeek、OpenAI）对话的唯一底层封装。
上层所有 Agent 想要「让 AI 思考/回答」，都通过这里发请求，不要各自直接调 API。
这样做的好处：密钥、模型名、超时、流式解析逻辑只写一份，换模型只改配置。

对外提供 3 个核心方法：
  - chat_json    ：让模型返回「结构化 JSON」（适合让 AI 输出固定格式的分析结果）
  - chat_text    ：让模型返回「一段自由文本」（适合写总结、解释）
  - chat_text_stream：让模型「一个字一个字往外蹦」地流式返回（聊天界面那种打字机效果）

设计上的「为什么」：
  - 每个方法先判断 api_key 是否为空，空就直接返回空结果——这样没配密钥时程序
    不会崩，而是优雅降级（比如用规则兜底）。
  - 用 try/except 包住网络请求：模型服务偶尔抽风，捕获异常返回空值，比整页报错好。."""

import json  # 用来把 Python 字典和 JSON 字符串互相转换
from collections.abc import AsyncIterator  # 异步迭代器类型标注，用于流式返回
from typing import Any  # 表示任意类型

import httpx  # 异步 HTTP 客户端

from app.core.config import settings  # 全局配置：模型地址、密钥、模型名
from app.services.context_compression import prompt_cache_model_kwargs  # 给某些模型开启「提示词缓存」的额外参数


class LLMClient:
    def __init__(self) -> None:
        # 从配置读取三件套：base_url（模型服务地址）、api_key（密钥）、model（用哪个模型）
        self.base_url = settings.llm_base_url.rstrip("/")  # rstrip 去掉结尾斜杠，避免拼 URL 出错
        self.api_key = settings.llm_api_key
        self.model = settings.llm_model

    async def chat_json(self, system_prompt: str, user_payload: dict[str, Any]) -> dict[str, Any]:
        # 让模型输出 JSON。system_prompt=系统指令（你是谁、怎么答），user_payload=用户给的结构化数据
        if not self.api_key:
            return {}  # 没密钥直接返回空字典，避免后面发请求失败

        # 组装发给模型的标准请求体
        payload: dict[str, Any] = {
            "model": self.model,  # 用哪个模型
            "messages": [
                {"role": "system", "content": system_prompt},  # 系统角色：定规矩
                # 为什么把 user_payload 转成 JSON 字符串当 user 消息：保证复杂结构（嵌套字典）完整传给模型
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
            "response_format": {"type": "json_object"},  # 明确要求模型只返回 JSON 对象，方便我们解析
        }
        payload.update(prompt_cache_model_kwargs())  # 补上提示词缓存参数（支持的模型能省 token、提速）
        # 为什么用 AsyncClient 且 timeout=60：模型思考可能较慢，给 60 秒；异步不阻塞其他请求
        async with httpx.AsyncClient(timeout=60) as client:
            try:
                # 发 POST 到 /chat/completions（OpenAI 兼容接口的标准路径）
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}"},  # 用 Bearer 令牌认证
                    json=payload,
                )
                response.raise_for_status()  # 出错就抛异常
                # 模型返回的结构是 choices[0].message.content，里面是 JSON 字符串，再 json.loads 解析成字典
                content = response.json()["choices"][0]["message"]["content"]
                return json.loads(content)
            except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError):
                # 为什么捕获这么多异常：网络错、字段缺失、JSON 解析失败都可能发生，统一兜底返回空字典
                return {}

    async def chat_text(self, system_prompt: str, user_message: str) -> str:
        # 让模型返回纯文本（不强制 JSON）。user_message 这里是现成的字符串
        if not self.api_key:
            return ""  # 没密钥返回空串

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            # 注意这里没有 response_format，因为我们要自由文本
        }
        payload.update(prompt_cache_model_kwargs())
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError:
                return ""  # 模型报错就返回空串，不中断上层流程
            # 取出模型回复内容；.get("content") or "" 保证即使没有 content 字段也返回空串而不是 None
            return str(response.json()["choices"][0]["message"].get("content") or "")

    async def chat_text_stream(self, system_prompt: str, messages: list[dict[str, str]]) -> AsyncIterator[str]:
        # 流式输出：模型一边生成一边把片段（chunk）传回来，像打字机。用于聊天界面实时显示
        if not self.api_key:
            return  # 没密钥直接结束（异步生成器用 return 表示什么都不产出）

        # 把 system 指令拼到消息列表最前面；*messages 是「把列表元素展开」塞进去
        payload_messages = [{"role": "system", "content": system_prompt}, *messages]
        payload: dict[str, Any] = {"model": self.model, "messages": payload_messages, "stream": True}
        payload.update(prompt_cache_model_kwargs())
        async with httpx.AsyncClient(timeout=60) as client:
            # client.stream 是「边收边处理」的模式，不会等全部接收完
            async with client.stream(
                "POST",
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=payload,
            ) as response:
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError:
                    return  # 出错就停止产出

                # aiter_lines 逐行读取响应。SSE 格式里每行像：data: {"choices":[{"delta":{"content":"你"}}]}
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):  # 不是 data: 开头的行（如注释行）跳过
                        continue
                    data = line.removeprefix("data: ").strip()  # 去掉 "data: " 前缀并去首尾空格
                    if data == "[DONE]":  # 模型发完会发 [DONE] 表示结束
                        break
                    try:
                        chunk = json.loads(data)  # 把这一行解析成字典
                    except json.JSONDecodeError:
                        continue  # 解析失败就跳过这一行
                    # 从嵌套结构里取 delta.content（这一小段新生成的文字）
                    delta = chunk.get("choices", [{}])[0].get("delta", {})
                    content = delta.get("content")
                    if content:  # 只有真正有内容才产出，避免产出 None
                        yield str(content)  # yield 是生成器的「产出」，调用方用 async for 一点点收到
