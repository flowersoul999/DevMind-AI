"""DevMind AI module."""

# 导入「抽象基类」工具：ABC 用来定义不能被直接实例化的基类，abstractmethod 用来声明「子类必须实现」的方法
from abc import ABC, abstractmethod
# 导入 Any 类型：表示「任意类型」，当某个值的类型太灵活、不好精确描述时，就用 Any 兜底
from typing import Any


# 为什么：这是所有 Agent 的「公共父类」。
# 把 name / description / available_tools 和 run / validate_output / handle_error 抽到基类里，
# 就像公司给每个员工都规定好「姓名、岗位、能用的工具、上岗动作、出错处理」的模板，
# 这样后面写 ChatAgent、IssueAgent 等具体 Agent 时，只要专注实现自己的业务逻辑即可，避免重复造轮子。
class BaseAgent(ABC):
    """BaseAgent."""
    # name：每个 Agent 的唯一名字（字符串），例如 "chat_agent"，用于日志、路由、编排时识别「是谁」
    name: str
    # description：一句人话说明这个 Agent 负责什么，主要给调用方和其他 Agent 看，方便知道该交给谁
    description: str
    # available_tools：这个 Agent「声明」自己能使用的工具名列表（只是声明/记录，真正注册由上层完成）
    available_tools: list[str]

    # @abstractmethod：抽象方法标记。意思是「所有子类都必须实现 run 方法，否则 Python 会报错」。
    # 为什么：强制每个 Agent 都提供一个统一的入口 run()，编排层（如 WorkFlowOrchestrator）才能用相同方式调用它们。
    # async：异步函数（协程）。因为 Agent 内部经常要「等待」大模型返回、等待网络请求（GitHub API / LLM），
    #        用 async 可以让这些等待期间不阻塞服务器，同时处理其他请求，提升并发性能。
    # 参数说明：
    #   input_data：本次任务的输入数据（字典，键通常是 message / issue / pull_request 等）
    #   context：全局上下文（字典），携带 repo_id、team_members、evidence 等跨 Agent 共享信息
    # 返回值：dict[str, Any]，即 Agent 的输出，也是字典
    @abstractmethod
    async def run(self, input_data: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # 占位实现：抽象方法体里写 raise NotImplementedError，意思是「基类不干具体活，子类必须覆盖我」
        raise NotImplementedError

    # validate_output：对 Agent 的输出做「校验/兜底」的钩子（hook）。
    # 默认实现直接原样返回输出，等于「不做任何校验」。
    # 为什么：基类提供一个安全的默认行为，子类如果需要对输出做额外检查（比如补默认值、过滤敏感字段），可以重写它。
    def validate_output(self, output: dict[str, Any]) -> dict[str, Any]:
        # 直接把收到的输出原样交出去
        return output

    # handle_error：统一的「出错兜底」方法。当某个 Agent 执行失败时，可以用它生成一份结构化的错误信息，
    # 而不是让整个流程崩溃。这就像「员工出错时，先交一份事故报告，而不是直接跑路」。
    def handle_error(self, error: Exception, context: dict[str, Any]) -> dict[str, Any]:
        # 返回一份固定结构的错误字典：
        #   error：异常的可读描述（str(error) 把异常对象转成文字）
        #   agent：是哪个 Agent 出的错（self.name 就是当前实例的名字）
        #   context：把关键信息 repo_id 单独抽出来带上（如果有），方便排查是「哪个仓库」出错
        return {
            "error": str(error),
            "agent": self.name,
            "context": {"repo_id": str(context.get("repo_id")) if context.get("repo_id") else None},
        }
