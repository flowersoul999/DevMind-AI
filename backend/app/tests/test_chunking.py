"""DevMind AI module."""

# io：Python 标准库，用来在内存里造"假文件"（BytesIO），方便测试文档解压而不用真落盘。
import io
# zipfile：标准库，用来在测试里手工造一个 docx（本质是 zip 包），验证解压前的大小检查。
import zipfile

# pytest：测试框架。
import pytest

# 导入真实的分块模块（chunking）和文档抽取模块（document_extraction）。
# 为什么：这些函数把大文档切成小块供 RAG 检索，是核心逻辑，本测试不 mock，直接验证真实行为。
from app.services.rag import chunking, document_extraction
# 直接从 chunking 里导入三个最常用的分块函数。
from app.services.rag.chunking import chunk_code_text, chunk_document, chunk_text


def test_chunk_text_keeps_paragraphs_and_respects_size_limit() -> None:
    # 造一段长文本：三段，每段重复 8 遍，模拟真实文档里"一段讲一件事"的结构。
    text = "第一段说明登录流程。" * 8 + "\n\n" + "第二段说明刷新令牌。" * 8 + "\n\n" + "第三段说明退出流程。" * 8

    # 把文本切成每块最多 190 字、相邻块重叠 20 字的小块（重叠是为了不让一句话被切断后丢失上下文）。
    chunks = chunk_text(text, max_chars=190, overlap=20)

    # 断言：切出来的块数至少是 2（说明确实按大小切开了，没有整段原样返回）。
    assert len(chunks) >= 2
    # 断言：每块都不超过 190 字（验证尺寸上限真的生效）。
    assert all(len(chunk) <= 190 for chunk in chunks)
    # 断言：第一块里保留了段落分隔符 "\n\n"，说明分块没有把段落边界抹掉（否则检索时语境就乱了）。
    assert "\n\n" in chunks[0]


def test_chunk_text_rejects_invalid_overlap() -> None:
    # overlap（重叠）不能超过单块大小，否则逻辑无意义。这里故意传 overlap=100 等于 max_chars=100 来试探。
    try:
        # 这一行预期会抛 ValueError，所以放进 try。
        chunk_text("content", max_chars=100, overlap=100)
    except ValueError as exc:
        # 捕到异常后，确认错误信息里提到了 overlap（说明报错原因明确）。
        assert "overlap" in str(exc)
    else:
        # 如果没抛异常，说明函数没拦住非法参数——这里主动抛 AssertionError 让测试失败。
        # 为什么：用 else 分支兜底，确保"预期会报错"这件事真的被验证了，而不是函数静默通过了。
        raise AssertionError("expected ValueError")


def test_markdown_chunking_preserves_heading_context_and_code_fences() -> None:
    # 一段 markdown：有大标题、小标题，还有一个 python 代码围栏（```python ... ```）。
    text = """# Authentication

The service verifies access tokens before every request.

## Refresh tokens

Refresh tokens are rotated after a successful exchange.

```python
def rotate_token(token: str) -> str:
    return token + "-rotated"
```."""

    # 按 markdown 文档分块，单块上限 180 字。
    chunks = chunk_document(text, "auth.md", max_chars=180, overlap=20)

    # 断言：确实分出了块（非空）。
    assert chunks
    # 断言：每块内容都不超过 180 字。
    assert all(len(chunk["content"]) <= 180 for chunk in chunks)
    # 断言：至少有一块记录了自己的"标题路径"是 ["Authentication", "Refresh tokens"]，说明分块保留了多级标题上下文。
    assert any(chunk["section_path"] == ["Authentication", "Refresh tokens"] for chunk in chunks)
    # 找到类型为 markdown_code 的那块（即代码围栏对应的块）。
    code_chunk = next(chunk for chunk in chunks if chunk["chunk_type"] == "markdown_code")
    # 断言：代码块里恰好有 2 个 ```（开头一个、结尾一个），说明围栏被完整保留、没被拆坏。
    assert code_chunk["content"].count("```") == 2
    # 断言：代码块记下了它属于哪个小标题（Refresh tokens）。
    assert code_chunk["section_title"] == "Refresh tokens"
    # 断言：代码块的起始行号 <= 结束行号（行号必须是合理的、可排序的）。
    assert code_chunk["start_line"] <= code_chunk["end_line"]


def test_markdown_overlap_starts_at_a_readable_boundary() -> None:
    # 短 markdown，用来精确验证"重叠部分从哪个词开始"。
    text = "# A\n\nalpha bravo charlie delta echo foxtrot\n\nsecond evidence"

    chunks = chunk_document(text, "overlap.md", max_chars=55, overlap=20)

    # 断言：切成了正好 2 块。
    assert len(chunks) == 2
    # 取第二块正文（去掉第一段空行分隔），看它开头是哪个词。
    overlap_body = chunks[1]["content"].split("\n\n", 1)[1]
    # 断言：重叠衔接处落在 charlie 或 delta 这种完整单词上，而不是把一个单词从中间劈开（保证可读性）。
    assert overlap_body.split()[0] in {"charlie", "delta"}


def test_markdown_chunk_ids_are_stable_for_the_same_content() -> None:
    # 同一份内容，分两次块，验证每次生成的 chunk_id / parent_id 完全一致。
    text = "# Deploy\n\nRun the migration before restarting the API."

    first = chunk_document(text, "runbook.md", max_chars=200, overlap=20)
    second = chunk_document(text, "runbook.md", max_chars=200, overlap=20)

    # 断言：两次的 chunk_id 列表完全相同——意味着同一份文档在 RAG 里重建时不会"换身份证"，可被稳定索引/去重。
    assert [chunk["chunk_id"] for chunk in first] == [chunk["chunk_id"] for chunk in second]
    # 断言：父块 id 也一致（层级关系稳定）。
    assert [chunk["parent_id"] for chunk in first] == [chunk["parent_id"] for chunk in second]


def test_python_chunking_uses_ast_symbol_boundaries() -> None:
    # 一段 python 源码：import、一个函数、一个类。
    source = """import os

def load_settings() -> dict:
    return {"environment": os.getenv("ENV", "dev")}

class TokenService:
    def issue(self, user_id: str) -> str:
        return f"token:{user_id}"."""

    # 按代码分块（会用 AST 语法树识别函数/类边界）。
    chunks = chunk_code_text(source, "app/token_service.py", max_chars=220)

    # 断言：切出来的符号集合至少包含 module（整文件）、load_settings（函数）、TokenService（类）。
    assert {chunk["symbol"] for chunk in chunks} >= {"module", "load_settings", "TokenService"}
    # 取出 load_settings 函数对应的块。
    function = next(chunk for chunk in chunks if chunk["symbol"] == "load_settings")
    # 断言：它的 symbol_kind 是 function（分类正确）。
    assert function["symbol_kind"] == "function"
    # 断言：起始行号是 3（对应源码里 def load_settings 那一行）。
    assert function["start_line"] == 3
    # 断言：块的头部以 "File: app/token_service.py" 开头（便于检索时知道代码来自哪个文件）。
    assert function["header"].startswith("File: app/token_service.py")


def test_typescript_chunking_recognizes_exported_functions_and_arrow_functions() -> None:
    # 一段 TypeScript：普通 export 函数和箭头函数两种写法。
    source = """import { api } from './api';

export async function loadUser(id: string) {
  return api.get(`/users/${id}`);
}

export const saveUser = async (id: string) => {
  return api.post(`/users/${id}`);
};."""

    chunks = chunk_code_text(source, "src/users.ts", max_chars=220)

    # 断言：符号集合至少包含 module、loadUser、saveUser（两种函数写法都被识别）。
    assert {chunk["symbol"] for chunk in chunks} >= {"module", "loadUser", "saveUser"}
    # 断言：所有块都标记 language == "ts"（语言识别正确）。
    assert all(chunk["language"] == "ts" for chunk in chunks)


def test_invalid_python_falls_back_without_losing_content() -> None:
    # 故意写一段语法错误的 python（def broken( 括号没闭合）。
    source = "def broken(:\n    return 1\n"

    chunks = chunk_code_text(source, "broken.py", max_chars=80)

    # 断言：即使 AST 解析失败，也至少返回了一块（兜底逻辑生效，不会直接崩）。
    assert chunks
    # 断言：原文 "def broken" 仍然保留在拼接后的内容里（没有因为解析失败就丢内容）。
    assert "def broken" in "\n".join(chunk["content"] for chunk in chunks)
    # 断言：第一块的符号是 module（退化成整文件块）。
    assert chunks[0]["symbol"] == "module"


def test_csv_chunking_repeats_header_and_tracks_row_ranges() -> None:
    # 一段 CSV：表头 id,name，下面 4 行数据。
    text = "id,name\n1,Alice\n2,Bob\n3,Carol\n4,David"

    chunks = chunk_document(text, "users.csv", max_chars=25, overlap=5)

    # 断言：至少切出 2 块。
    assert len(chunks) >= 2
    # 断言：每块的第一行都是表头 id,name——CSV 切块必须把表头重复到每块，否则单块失去列含义。
    assert all(chunk["content"].splitlines()[0] == "id,name" for chunk in chunks)
    # 断言：块类型都是 csv_rows。
    assert all(chunk["chunk_type"] == "csv_rows" for chunk in chunks)
    # 断言：每块的起始行号 <= 结束行号。
    assert all(chunk["row_start"] <= chunk["row_end"] for chunk in chunks)


def test_small_json_manifest_stays_in_one_chunk() -> None:
    # 一个很小的 JSON（package.json 风格），尺寸足够小。
    chunks = chunk_document('{"name":"demo","version":"1.0.0"}', "package.json", max_chars=200, overlap=20)

    # 断言：只切成 1 块（小 JSON 不强行拆分，保持结构完整）。
    assert len(chunks) == 1
    # 断言：json_path 是 "$"（根路径）。
    assert chunks[0]["json_path"] == "$"
    # 断言：内容里保留了 '"name": "demo"'。
    assert '"name": "demo"' in chunks[0]["content"]


def test_plain_text_overlap_is_present_and_line_numbers_keep_leading_blanks() -> None:
    # 纯文本：前面留两个空行，后面接 000~099 共 100 行数字。
    text = "\n\n" + "".join(f"{index:03d}" for index in range(100))

    chunks = chunk_document(text, "notes.txt", max_chars=60, overlap=12)

    # 断言：切出超过 2 块（因为内容长）。
    assert len(chunks) > 2
    # 断言：第一块起始行号是 3——前面两个空行被算进了行号，证明行号保留了前导空行（不丢计数）。
    assert chunks[0]["start_line"] == 3
    # 断言：每块结尾 12 字 == 下一块开头 12 字，正是 overlap=12 的体现（相邻块确实重叠了 12 字）。
    assert all(previous["content"][-12:] == current["content"][:12] for previous, current in zip(chunks, chunks[1:]))


def test_csv_long_rows_are_split_without_data_loss() -> None:
    # CSV：某一行 data 字段长达 80 个 x，远超单块上限 20 字。
    text = "id,data\n1," + "x" * 80 + "\n2,ok"

    chunks = chunk_document(text, "long.csv", max_chars=20, overlap=2)

    # 断言：每块都不超过 20 字。
    assert all(len(chunk["content"]) <= 20 for chunk in chunks)
    # 断言：所有块里 x 的总数加起来还是 80——长行被切成多块，但字符一个没丢（无损拆分）。
    assert sum(chunk["content"].count("x") for chunk in chunks) == 80
    # 断言：被拆成多 part 的行，其 row_start 集合里包含 2（即那行长行被标记为第 2 行、分了多段）。
    assert {chunk["row_start"] for chunk in chunks if chunk.get("row_part")} == {2}


def test_csv_fields_larger_than_the_stdlib_default_remain_structured() -> None:
    # 造一个字段长达 14 万个 x 的 CSV 行——这超过 Python 标准库 csv 的默认字段上限，普通解析会炸。
    rendered_row = "1," + "x" * 140_000

    chunks = chunk_document(f"id,data\n{rendered_row}", "large-field.csv", max_chars=5000, overlap=0)

    # 把所有数据行（去掉各自第一行表头）拼回去，应该和原始长行完全一致——证明超大字段也能完整、结构化地处理。
    reconstructed = "".join(chunk["content"].split("\n", 1)[1] for chunk in chunks)
    assert reconstructed == rendered_row
    # 断言：所有块都是 csv_rows 类型。
    assert all(chunk["chunk_type"] == "csv_rows" for chunk in chunks)


def test_csv_oversized_header_is_lossless_and_size_limited() -> None:
    # 造一个很长的表头（column_ 重复 8 次）。
    header = "column_" * 8

    chunks = chunk_document(f"{header}\nvalue", "header.csv", max_chars=20, overlap=2)

    # 取出所有 csv_header_part 类型的块（表头被拆成的片段）。
    header_chunks = [chunk for chunk in chunks if chunk["chunk_type"] == "csv_header_part"]
    # 断言：把这些表头片段拼起来，正好等于原始长表头（无损）。
    assert "".join(chunk["content"] for chunk in header_chunks) == header
    # 断言：总有一块里包含数据行 "value"。
    assert any("value" in chunk["content"] for chunk in chunks)
    # 断言：每块都不超过 20 字（表头再长也被切小了）。
    assert all(len(chunk["content"]) <= 20 for chunk in chunks)


def test_repeated_and_empty_markdown_sections_remain_distinct() -> None:
    # markdown 里有两个同名 "## Auth" 小节，还有一个空的 "### Empty"。
    text = "# API\n\n## Auth\n\nfirst\n\n## Auth\n\nsecond\n\n### Empty"

    chunks = chunk_document(text, "api.md", max_chars=100, overlap=10)

    # 取出所有 section_title == "Auth" 的块。
    auth_chunks = [chunk for chunk in chunks if chunk["section_title"] == "Auth"]
    # 断言：有两个 Auth 块（同名小节不能合并成一块，要分别保留）。
    assert len(auth_chunks) == 2
    # 断言：这两个 Auth 块的 parent_id 不同——说明它们属于不同的父上下文，不会混淆。
    assert auth_chunks[0]["parent_id"] != auth_chunks[1]["parent_id"]
    # 断言：确实存在一个标题为 "Empty" 的块（空小节也被保留，而不是被丢弃）。
    assert any(chunk["section_title"] == "Empty" for chunk in chunks)
    # 断言：所有 chunk_id 互不相同（没有因为标题重复就生成重复的 id）。
    assert len({chunk["chunk_id"] for chunk in chunks}) == len(chunks)


def test_long_markdown_prefix_and_fence_obey_limit_with_exact_body_lines() -> None:
    # 造一个超长标题 + 一个带超长 info-string 的代码围栏。
    fence = "`" * 3
    text = "\n".join(
        [
            "# " + "H" * 200,
            "",
            fence + "python-with-a-very-long-info-string",
            "line1=1111",
            "line2=2222",
            "line3=3333",
            fence,
        ]
    )

    chunks = chunk_document(text, "fence.md", max_chars=30, overlap=2)

    # 断言：每块都不超过 30 字（连超长标题/info-string 都被限制住了）。
    assert all(len(chunk["content"]) <= 30 for chunk in chunks)
    # 找到包含 line2=2222 的块。
    line2_chunk = next(chunk for chunk in chunks if "line2=2222" in chunk["content"])
    # 断言：它的起止行号都是 5（精确对应源码第 5 行），说明行号计算没有被尺寸限制带偏。
    assert line2_chunk["start_line"] == 5
    assert line2_chunk["end_line"] == 5
    # 断言：围栏开始行号是 3（对应 ```python-... 那一行）。
    assert line2_chunk["fence_start_line"] == 3


def test_tiny_markdown_budget_falls_back_without_zero_width_fence_split() -> None:
    # 极小的预算 max_chars=8，连一个 ``` 围栏都装不下。
    text = "# A\n\n```\nvalue\n```"

    chunks = chunk_document(text, "tiny.md", max_chars=8, overlap=1)

    # 断言：至少返回了一块（极端小预算下也要兜底，不能返回空）。
    assert chunks
    # 断言：每块都不超过 8 字（预算被严格遵守）。
    assert all(len(chunk["content"]) <= 8 for chunk in chunks)


def test_duplicate_code_symbols_receive_unique_stable_ids() -> None:
    # 同一文件里定义了两个同名的 def same()（现实中少见但可能发生）。
    source = "def same():\n    pass\n\ndef same():\n    pass"

    first = chunk_code_text(source, "same.py", max_chars=100)
    second = chunk_code_text(source, "same.py", max_chars=100)
    # 取出所有 symbol == "same" 的块。
    same_chunks = [chunk for chunk in first if chunk["symbol"] == "same"]

    # 断言：有两个 same 块（同名函数都要保留，不能只留一个）。
    assert len(same_chunks) == 2
    # 断言：它们的 chunk_id 不同——靠"出现次序"区分，避免 id 撞车。
    assert same_chunks[0]["chunk_id"] != same_chunks[1]["chunk_id"]
    # 断言：两次运行得到的 id 列表一致（稳定可复现）。
    assert [chunk["chunk_id"] for chunk in first] == [chunk["chunk_id"] for chunk in second]


def test_large_json_does_not_claim_invented_source_lines() -> None:
    # 一段被切得很碎的大 JSON。
    text = '{\n  "first": "' + "a" * 40 + '",\n  "second": "' + "b" * 40 + '"\n}'

    chunks = chunk_document(text, "data.json", max_chars=30, overlap=3)

    # 断言：切成多于 1 块。
    assert len(chunks) > 1
    # 断言：所有块的 start_line / end_line 都是 None。
    # 为什么：JSON 是按结构切的、不是按行切的，这里故意不编造假行号，避免检索时给出错误源码定位（宁可没行号也不能骗人）。
    assert all(chunk["start_line"] is None and chunk["end_line"] is None for chunk in chunks)


def test_text_extraction_rejects_binary_and_excessive_output(monkeypatch: pytest.MonkeyPatch) -> None:
    # monkeypatch 是 pytest 的"临时替换"工具：能在测试期间把某个变量/函数改掉，测试结束自动还原，不影响别的测试。
    # 这里把解码字符上限临时改到 8，方便触发"超长"分支。
    monkeypatch.setattr(document_extraction, "MAX_EXTRACTED_CHARS", 8)

    # 预期 decode_text 会因超长抛 ValueError，且错误信息含 "character limit"。
    with pytest.raises(ValueError, match="character limit"):
        document_extraction.decode_text(b"more than eight characters")
    # 预期遇到含 \x00 的二进制字节时抛 ValueError，含 "binary"。
    with pytest.raises(ValueError, match="binary"):
        document_extraction.decode_text(b"abc\x00def")


def test_docx_archive_expansion_is_checked_before_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    # 在内存里造一个 docx（zip 包），里面塞 32 字节的 document.xml。
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", "x" * 32)
    # 把"解压后最大字节数"临时限制成 16，触发炸弹(zip bomb)防御。
    monkeypatch.setattr(document_extraction, "MAX_DOCX_UNCOMPRESSED_BYTES", 16)

    # 预期解压前的大小检查会拦住它，抛 ValueError 含 "expanded size"。
    with pytest.raises(ValueError, match="expanded size"):
        document_extraction.extract_document_text("bomb.docx", buffer.getvalue())


def test_pdf_page_limit_is_checked_before_page_extraction(monkeypatch: pytest.MonkeyPatch) -> None:
    # 在内存里生成一个有 2 页的 PDF。
    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    writer.add_blank_page(width=100, height=100)
    writer.write(buffer)
    # 把"最大页数"临时限制成 1。
    monkeypatch.setattr(document_extraction, "MAX_PDF_PAGES", 1)

    # 预期在真正抽页之前，页数检查就拦住它，抛 ValueError 含 "page limit"。
    with pytest.raises(ValueError, match="page limit"):
        document_extraction.extract_document_text("many-pages.pdf", buffer.getvalue())


def test_document_chunk_count_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    # 把"单文档最大块数"临时限制成 2。
    monkeypatch.setattr(chunking, "MAX_CHUNKS_PER_DOCUMENT", 2)

    # 故意用 max_chars=2 去切一个长串，必然想切成很多块——但上限会拦住，抛 ValueError 含 "chunk limit"。
    with pytest.raises(ValueError, match="chunk limit"):
        chunking.chunk_document("abcdef", "tiny.txt", max_chars=2, overlap=0)
