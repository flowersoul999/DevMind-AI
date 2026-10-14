"""
services/llm/structured_outputs.py —— 结构化输出 schema 的「统一再导出」
============================================================================

这个文件本身几乎没逻辑，它做的事很简单：把分析 Agent 们要用的「输出格式契约」
（Pydantic 模型）从 schemas/analysis.py 重新导出，并登记到 __all__。

为什么要多这么一层？
--------------------
上层代码（比如某个 Agent）想用「标准的分析结果格式」时，可以统一从
llm.structured_outputs 导入，而不用关心这些定义具体写在哪个 schema 文件里。
这等于在前厅放了一个「目录牌」，告诉别人「要这些标准格式，来我这拿」。
好处是：如果以后分类调整，只改这一处导出即可。

下面这些名字的含义（都是分析结果的数据结构）：
  - CIDebugOutput      ：CI 调试 Agent 的输出（失败类型、根因、修复步骤）
  - IssueAnalysisOutput：Issue 分诊 Agent 的输出（分类、优先级、推荐负责人）
  - PRReviewFinding    ：PR 审查里的一条具体发现
  - PRReviewOutput     ：PR 审查 Agent 的完整输出
  - SafetyOutput       ：安全 Agent 的输出（某写操作是否需人工确认）."""

# 从分析 schema 文件里把这几个「输出数据结构」引进来
from app.schemas.analysis import CIDebugOutput, IssueAnalysisOutput, PRReviewFinding, PRReviewOutput, SafetyOutput

# __all__ 声明「本模块对外公开的名字」，配合 from xxx import * 时使用，也方便 IDE 自动提示
__all__ = [
    "CIDebugOutput",
    "IssueAnalysisOutput",
    "PRReviewFinding",
    "PRReviewOutput",
    "SafetyOutput",
]
