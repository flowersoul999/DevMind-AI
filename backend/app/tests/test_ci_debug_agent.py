"""DevMind AI module."""

# 导入 pytest 测试框架（提供 @pytest.mark.asyncio 等装饰器和断言能力）。
# 为什么：这是所有 pytest 测试文件都需要的"地基"，没有它就无法识别测试用例、也无法跑异步测试。
import pytest

# 从项目里导入真实的 CIDebugAgent（CI 调试智能体），它不是 mock，而是真要跑的代码。
# 为什么：本测试想验证 Agent 真实的失败分类能力，所以故意不 mock，让它真去分析一份伪造的 CI 失败数据。
from app.services.agents.ci_debug_agent import CIDebugAgent


# @pytest.mark.asyncio 告诉 pytest："这个函数里有 await，请用异步方式运行我"。
# 为什么：Python 的 async 函数不能直接调用，必须放进事件循环里跑；不标这个装饰器，pytest 会不知道怎么执行它而报错。
# 函数名以 test_ 开头，pytest 才会把它当成一条测试用例自动收集。
@pytest.mark.asyncio
async def test_ci_debug_agent_includes_failed_job_context() -> None:
    # 调用真实 Agent 的 run 方法，喂给它一份"模拟的 GitHub CI 失败结果"。
    # 为什么：我们不想真的去跑 CI，所以手搓一份和真实 GitHub Actions 返回结构一致的字典，既能触发逻辑又完全可控。
    result = await CIDebugAgent().run(
        {
            # name：这次 CI 工作流的名称。
            "name": "backend-test",
            # status / conclusion：整体状态是"完成"但结论是"失败"。
            "status": "completed",
            "conclusion": "failure",
            # jobs：一个失败的任务列表，里面嵌套了 step（步骤），模拟 pytest 这步挂了。
            "jobs": [{"name": "pytest", "conclusion": "failure", "steps": [{"name": "Run tests", "conclusion": "failure"}]}],
            # logs_text：模拟的失败日志，包含 pytest 报错关键字，用来验证 Agent 能不能从日志里抓到关键信息。
            "logs_text": "pytest tests/test_api.py FAILED AssertionError",
        },
        # 第二个参数是额外的上下文（这里传空字典），保持接口完整调用。
        {},
    )
    # assert 就是"交卷前的自检清单"：如果这句话不成立，测试立刻判不及格（红）。
    # 这里断言：Agent 把失败类型正确识别成了 "test"（测试失败），而不是误判成编译、配置等其他类型。
    assert result["failure_type"] == "test"
    # 断言：失败摘要里必须包含 "pytest" 关键字，说明 Agent 确实读懂了日志、定位到了 pytest 失败。
    assert "pytest" in result["failure_summary"]
