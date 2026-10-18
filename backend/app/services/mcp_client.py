"""
services/mcp_client.py —— 启动并对话「记忆 MCP 服务器」的客户端
============================================================================

MCP（Model Context Protocol，模型上下文协议）是 Anthropic 提出的标准：让 AI
模型能像插 U 盘一样，按需调用外部工具/数据源。本项目把「记忆检索」做成了一个
MCP 服务器（memory_server.py）。本文件就是「前端网页侧的 Agent」用来和那个
服务器对话的客户端。

通信方式：stdio（标准输入/输出）。类比——本文件新开一个 Python 子进程跑
memory_server.py，然后通过「往它的 stdin 写 JSON、从它的 stdout 读 JSON」来
一问一答。协议用的是 JSON-RPC 2.0（一种远程调用的标准格式）。

对外方法：
  - call_tool(name, arguments) ：调用服务器上的某个工具（如搜索证据），用完关进程
  - inspect_server()           ：和服务器握手并列出它支持哪些工具（用于前端展示）."""

import asyncio  # 异步 IO，用于启动子进程并和它通信
import json  # 把字典和 JSON 字符串互转
import os  # 操作环境变量
import sys  # 获取当前 Python 解释器路径
from pathlib import Path  # 优雅地处理文件路径
from typing import Any  # 任意类型

from app.core.config import settings  # 全局配置（数据库地址、是否自动同步等）


class McpClientError(RuntimeError):
    """McpClientError."""
    # 自定义异常：凡是 MCP 通信出问题（超时、进程崩了、返回非法 JSON）都抛它
    # 为什么继承 RuntimeError：表示「运行时才会出现的错误」，和内置异常体系一致，方便上层 catch
    pass


class StdioMcpClient:
    def __init__(self, command: str | None = None, args: list[str] | None = None, cwd: str | None = None) -> None:
        # 计算 backend 根目录：本文件在 services/ 下，parents[2] 就是 backend/ 的上两级
        backend_root = Path(__file__).resolve().parents[2]
        # command 默认用「当前 Python 解释器」来跑子进程；args 默认指向 memory_server.py
        self.command = command or sys.executable
        # 为什么用列表传参数：asyncio 要求命令和参数分开，避免 shell 注入风险
        self.args = args or [str(backend_root / "app" / "mcp" / "memory_server.py")]
        self.cwd = cwd or str(backend_root)  # 子进程的工作目录，默认 backend 根
        self._next_id = 1  # JSON-RPC 的请求编号，每次发请求自增，用来配对「请求-响应」

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        # 一次完整的「调用工具」流程：启动→初始化握手→通知已就绪→发工具调用→关进程
        process = await self._start()
        try:
            await self._initialize(process)  # 第一步：和服务器商量协议版本
            await self._notify(process, "notifications/initialized", {})  # 第二步：告诉服务器「我初始化好了」
            # 第三步：真正调用工具，name 是工具名，arguments 是参数
            return await self._request(process, "tools/call", {"name": name, "arguments": arguments})
        finally:
            # finally 保证无论成功失败都会关掉子进程，避免进程泄漏（像用完拔掉 U 盘）
            await self._stop(process)

    async def inspect_server(self) -> dict[str, Any]:
        # 探测服务器：握手 + 列工具。用于前端「MCP 运行状态」展示
        """Perform a real MCP handshake and return the server's advertised tools."""
        process = await self._start()
        try:
            initialize = await self._initialize(process)
            await self._notify(process, "notifications/initialized", {})
            tools = await self._request(process, "tools/list", {})  # 问服务器：你有哪些工具？
            # 返回握手结果 + 工具列表；.get("tools") or [] 防止字段缺失
            return {"initialize": initialize, "tools": tools.get("tools") or []}
        finally:
            await self._stop(process)

    async def _initialize(self, process: asyncio.subprocess.Process) -> dict[str, Any]:
        # JSON-RPC 的 initialize：声明我们用的协议版本和客户端信息
        return await self._request(
            process,
            "initialize",
            {
                "protocolVersion": "2024-11-05",  # MCP 协议版本号，服务器和客户端要对上
                "capabilities": {},  # 我们这边不额外声明能力
                "clientInfo": {"name": "devflow-web-agent", "version": "0.1.0"},  # 自报家门
            },
        )

    async def _start(self) -> asyncio.subprocess.Process:
        # 真正启动子进程（memory_server.py）
        env = os.environ.copy()  # 复制当前环境变量
        env["PYTHONIOENCODING"] = "utf-8"  # 强制子进程用 UTF-8 输入输出，避免中文乱码
        env["DATABASE_URL"] = settings.database_url  # 把数据库连接串传给子进程，它才能查库
        env.setdefault("AUTO_SYNC_ENABLED", "false")  # 子进程里关掉自动同步，防止它自作主张去拉 GitHub
        # create_subprocess_exec：以可执行文件方式启动（不是 shell），更安全和跨平台
        process = await asyncio.create_subprocess_exec(
            self.command,
            *self.args,  # 把参数列表展开传给子进程
            cwd=self.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,  # 拿到子进程的 stdin 管道（我们往里写）
            stdout=asyncio.subprocess.PIPE,  # 拿到 stdout 管道（我们从这读）
            stderr=asyncio.subprocess.PIPE,  # stderr 管道（出错信息从这读）
        )
        return process

    async def _notify(self, process: asyncio.subprocess.Process, method: str, params: dict[str, Any]) -> None:
        # 通知（notification）：一种「不用等回复」的消息。这里用来发 initialized 通知
        await self._write(process, {"jsonrpc": "2.0", "method": method, "params": params})

    async def _request(self, process: asyncio.subprocess.Process, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # 发送一个「请求」并等待「响应」。这是核心的收发逻辑
        request_id = self._next_id  # 取当前编号
        self._next_id += 1  # 编号自增，保证每个请求 id 唯一
        # 写出的 JSON 含 jsonrpc 版本、请求 id、方法名、参数——标准的 JSON-RPC 请求格式
        await self._write(process, {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        if process.stdout is None:
            raise McpClientError("MCP process has no stdout")  # 防御：管道没开就报错
        try:
            # 读服务器回的一行；wait_for 设 30 秒超时，防止服务器卡死导致我们永远等
            line = await asyncio.wait_for(process.stdout.readline(), timeout=30)
        except TimeoutError as exc:
            raise McpClientError(f"MCP request timed out: {method}") from exc
        if not line:
            # 读到空行说明服务器把 stdout 关了（进程可能崩了），把 stderr 也读出来帮定位
            stderr = ""
            if process.stderr is not None:
                stderr = (await process.stderr.read()).decode("utf-8", errors="replace")
            raise McpClientError(f"MCP server closed stdout. {stderr}")
        try:
            response = json.loads(line.decode("utf-8"))  # 把这一行 JSON 文本解析成字典
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            # 解析失败说明服务器返回了非 JSON（可能是报错信息），包装成我们的异常
            raise McpClientError(f"MCP server returned an invalid JSON response for {method}") from exc
        if "error" in response:
            # 服务器在响应里带了 error 字段，说明调用失败，把错误信息抛出来
            error = response["error"]
            raise McpClientError(str(error.get("message") or error))
        return response.get("result") or {}  # 成功则取 result 字段；没有就返回空字典

    async def _write(self, process: asyncio.subprocess.Process, payload: dict[str, Any]) -> None:
        # 把一条 JSON-RPC 消息写进子进程的 stdin
        if process.stdin is None:
            raise McpClientError("MCP process has no stdin")
        # 为什么加 "\n"：stdio 通信按行分隔，每行一条 JSON；encode 转成字节才能写入
        process.stdin.write((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        await process.stdin.drain()  # drain 确保数据真正发送出去（异步写需要这一步）

    async def _stop(self, process: asyncio.subprocess.Process) -> None:
        # 关停子进程：如果还在跑就 kill，然后 wait 等它真正退出，释放资源
        if process.returncode is None:  # returncode 为 None 表示还没结束
            process.kill()
        await process.wait()
