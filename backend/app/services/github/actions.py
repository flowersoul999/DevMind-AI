"""
services/github/actions.py —— GitHub Actions（CI/CD 流水线）数据获取层
============================================================================

这个文件专门负责和 GitHub 的「Actions」打交道。GitHub Actions 就是项目里跑
自动化任务的地方：比如你一提交代码，GitHub 就会自动编译、跑测试、部署，这些
自动任务就叫「workflow run（一次流水线运行）」。

为什么单独放一个文件？
----------------------
DevMind AI 要能「诊断 CI 为什么挂了」，就得先把 GitHub 上的流水线运行记录、
每一步 job、以及最关键的「日志」拉下来。这些和「issue / PR」是不同的一类
接口，所以单独成文件，方便维护。

本文件对外提供 3 个异步函数：
  - list_workflow_runs        ：列出某仓库最近的流水线运行（成功/失败/进行中）
  - list_workflow_run_jobs    ：列出某次运行里包含的各个 job（编译、测试…）
  - get_workflow_run_logs     ：把某次运行的日志打包下载并拼成一段文本

它们都依赖同目录的 GitHubClient（真正的 HTTP 请求发送者）。
"""

from io import BytesIO  # 把字节串当成「文件」来读，方便解压 zip（Python 标准库）
from zipfile import BadZipFile, ZipFile  # 处理 GitHub 返回的日志压缩包；BadZipFile 是解压失败的异常

from app.services.github.client import GitHubClient  # 引入底层 GitHub REST 客户端，所有请求都通过它发出


async def list_workflow_runs(owner: str, repo: str, token: str | None = None, limit: int = 30, base_url: str | None = None) -> list[dict]:
    # 为什么分开写参数：owner/repo 是「谁/哪个仓库」，token 是访问凭证，limit 控制拉多少条，base_url 支持自建 GitHub
    client = GitHubClient(token, base_url)  # 新建一个客户端实例；如果没传 token 就用全局配置里的
    # 调用 GitHub 接口：/repos/{owner}/{repo}/actions/runs 是「列出流水线运行」的官方路径
    payload = await client.get(f"/repos/{owner}/{repo}/actions/runs", {"per_page": limit})
    # GitHub 返回的数据里，真正的列表在 "workflow_runs" 这个字段，.get 避免字段缺失时报错
    return payload.get("workflow_runs", [])


async def list_workflow_run_jobs(owner: str, repo: str, run_id: int, token: str | None = None, base_url: str | None = None) -> list[dict]:
    # run_id 是某一次具体运行的编号（就像一场考试的准考证号）
    client = GitHubClient(token, base_url)
    # 这次请求的是「某次运行下的 jobs」（一个流水线里往往有多个并行 job：编译、单测、lint…）
    payload = await client.get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs", {"per_page": 100})
    return payload.get("jobs", [])  # 取出 jobs 列表，没有就返回空列表


async def get_workflow_run_logs(owner: str, repo: str, run_id: int, token: str | None = None, base_url: str | None = None, max_chars: int = 80000) -> str:
    # max_chars 是「最多读多少字」的保护阀，防止日志太大把内存撑爆（类比：只听录音的前 8 万字）
    client = GitHubClient(token, base_url)
    # get_bytes 表示要的是「原始字节」而不是 JSON——因为日志是打包成 zip 下载的
    payload = await client.get_bytes(f"/repos/{owner}/{repo}/actions/runs/{run_id}/logs")
    try:
        # GitHub 把日志做成一个 zip 压缩包发回来，这里像拆快递一样把它解开
        with ZipFile(BytesIO(payload)) as archive:  # BytesIO 把字节串伪装成一个文件，让 ZipFile 能读
            parts: list[str] = []  # 用来收集每个日志文件的内容
            for name in sorted(archive.namelist()):  # namelist 是压缩包里所有文件名；sorted 保证顺序稳定
                if name.endswith("/"):  # 以斜杠结尾的是「文件夹」不是文件，跳过
                    continue
                raw = archive.read(name)  # 读出这个文件的原始字节
                text = raw.decode("utf-8", errors="replace")  # 字节转成文字；replace 表示遇到乱码用占位符代替，不让程序崩
                parts.append(f"### {name}\n{text}")  # 用 markdown 的标题标出文件名，再接内容，方便 LLM 阅读
                # 为什么用 sum 累加长度来判断：每加一个就检查总量，超过上限就停，避免无限读取
                if sum(len(part) for part in parts) >= max_chars:
                    break
            # 用空行把各段日志拼起来，再截断到 max_chars；[:max_chars] 是「切片」，只取前 max_chars 个字符
            return "\n\n".join(parts)[:max_chars]
    except BadZipFile:
        # 极少数情况日志不是 zip（比如返回了错误信息文本），直接当纯文本处理
        return payload.decode("utf-8", errors="replace")[:max_chars]
