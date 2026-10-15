"""DevMind AI module."""

from __future__ import annotations

import re
import zipfile
from io import BytesIO
from pathlib import Path


# 各种「安全上限」：防止超大 / 恶意文件把内存或服务拖垮。
# 类比：入口安检的行李尺寸限制，超过就拒收。
MAX_DOCUMENT_BYTES = 12 * 1024 * 1024          # 原始文件最大 12 MB
MAX_EXTRACTED_CHARS = 4_000_000                # 抽出纯文本最多 400 万字符
MAX_PDF_PAGES = 500                            # PDF 最多 500 页
MAX_DOCX_ENTRIES = 10_000                      # DOCX 内部条目（段落 / 表格等）上限
MAX_DOCX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024  # DOCX 解压后体积上限 64 MB


def _ensure_text_limit(text: str) -> str:
    # 把超长文本挡在门外，超过上限直接报错而不是默默截断。
    if len(text) > MAX_EXTRACTED_CHARS:
        raise ValueError(f"extracted text exceeds the {MAX_EXTRACTED_CHARS} character limit")
    return text


def decode_text(data: bytes) -> str:
    # 把「纯文本类」文件（txt / md / log / json / csv 等）的二进制字节解码成字符串。
    # 为什么：不同文件可能用不同编码保存，先嗅探是否有二进制内容，再按优先级尝试多种编码，保证中文不乱码。
    if b"\x00" in data[:8192]:
        # 前 8KB 里出现空字节，基本是二进制（不是纯文本），不能当文本索引
        raise ValueError("binary content cannot be indexed as text")
    # 依次尝试：带 BOM 的 UTF-8 → 纯 UTF-8 → 中文常用的 GB18030 → 最后兜底 Latin-1（几乎不会失败）
    for encoding in ["utf-8-sig", "utf-8", "gb18030", "latin-1"]:
        try:
            return _ensure_text_limit(data.decode(encoding))
        except UnicodeDecodeError:
            continue  # 该编码解不出，换下一个
    return ""  # 全部失败（极少见）返回空串


def _extract_pdf(data: bytes) -> str:
    # 从 PDF 二进制里抽取纯文本。需要 pypdf 这个第三方库。
    from pypdf import PdfReader

    reader = PdfReader(BytesIO(data))  # 用内存字节流打开 PDF，不落盘
    if len(reader.pages) > MAX_PDF_PAGES:
        raise ValueError(f"PDF exceeds the {MAX_PDF_PAGES} page limit")
    pages: list[str] = []
    extracted_chars = 0
    for page_number, page in enumerate(reader.pages, start=1):
        text = (page.extract_text() or "").strip()  # 抽取单页文字，可能为空
        if text:
            # 在每页文字前插入一个 HTML 注释标记页码，方便后续切片 / 引用时知道出自第几页
            content = f"<!-- page: {page_number} -->\n{text}"
            extracted_chars += len(content) + (2 if pages else 0)
            if extracted_chars > MAX_EXTRACTED_CHARS:
                raise ValueError(f"PDF text exceeds the {MAX_EXTRACTED_CHARS} character limit")
            pages.append(content)
    return _ensure_text_limit("\n\n".join(pages))  # 多页之间用空行隔开拼成一篇


def _paragraph_as_markdown(paragraph) -> str:
    # 把一个 Word 段落转成 Markdown 文本：标题、列表、普通段落分别处理。
    text = (paragraph.text or "").strip()
    if not text:
        return ""  # 空段落直接返回空
    # Word 用「样式名」区分标题级别 / 列表，从样式名里找 heading 几
    style_name = str(getattr(getattr(paragraph, "style", None), "name", "") or "")
    heading = re.match(r"heading\s+(\d+)", style_name, re.IGNORECASE)
    if heading:
        # 是标题：按级别转成 Markdown 的 # / ## / ### …（最多 6 级）
        level = min(6, max(1, int(heading.group(1))))
        return f"{'#' * level} {text}"
    lowered = style_name.lower()
    if "list bullet" in lowered:
        return f"- {text}"    # 项目符号列表 → Markdown 无序列表
    if "list number" in lowered:
        return f"1. {text}"   # 编号列表 → Markdown 有序列表
    return text  # 普通段落原样返回


def _table_as_markdown(table) -> str:
    # 把一个 Word 表格转成 Markdown 表格语法。
    # 先把每行每格的文字取出，换行替换成空格避免破坏表格结构
    rows = [[cell.text.strip().replace("\n", " ") for cell in row.cells] for row in table.rows]
    if not rows:
        return ""
    # 规整成矩形：补空格让每行列数一致（不齐的表也能正确渲染）
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]

    def render(row: list[str]) -> str:
        # 把一格内容里的竖线转义，再用 | 拼成一行 Markdown
        return "| " + " | ".join(cell.replace("|", "\\|") for cell in row) + " |"

    # 第一行是表头，第二行是分隔线（---），之后是数据行
    return "\n".join([render(normalized[0]), render(["---"] * width), *(render(row) for row in normalized[1:])])


def _extract_docx(data: bytes) -> str:
    # 从 .docx 二进制里抽取文本，并把段落 / 表格转成 Markdown。
    # .docx 本质是一个 zip 包，先像拆包裹一样检查它的大小和是否加密，防止 zip bomb 或恶意文件。
    try:
        with zipfile.ZipFile(BytesIO(data)) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_DOCX_ENTRIES:
                raise ValueError(f"DOCX exceeds the {MAX_DOCX_ENTRIES} entry limit")
            uncompressed_size = sum(info.file_size for info in infos)
            if uncompressed_size > MAX_DOCX_UNCOMPRESSED_BYTES:
                raise ValueError(
                    f"DOCX expanded size exceeds the {MAX_DOCX_UNCOMPRESSED_BYTES} byte limit"
                )
            if any(info.flag_bits & 0x1 for info in infos):
                # zip 条目标志位 0x1 表示加密，加密文档直接不支持
                raise ValueError("encrypted DOCX files are not supported")
    except zipfile.BadZipFile as exc:
        # 不是合法的 zip（损坏或非 docx）就报「无效归档」
        raise ValueError("invalid DOCX archive") from exc

    import docx
    from docx.oxml.table import CT_Tbl
    from docx.oxml.text.paragraph import CT_P
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    document = docx.Document(BytesIO(data))  # 真正解析 docx 文档对象
    blocks: list[str] = []
    extracted_chars = 0
    # Word 的 body 是一串段落 / 表格节点，按顺序遍历
    for child in document.element.body.iterchildren():
        if isinstance(child, CT_P):
            content = _paragraph_as_markdown(Paragraph(child, document))  # 段落节点
        elif isinstance(child, CT_Tbl):
            content = _table_as_markdown(Table(child, document))          # 表格节点
        else:
            content = ""  # 其它节点（如样式定义）忽略
        if content:
            extracted_chars += len(content) + (2 if blocks else 0)
            if extracted_chars > MAX_EXTRACTED_CHARS:
                raise ValueError(f"DOCX text exceeds the {MAX_EXTRACTED_CHARS} character limit")
            blocks.append(content)
    return _ensure_text_limit("\n\n".join(blocks))  # 各块用空行隔开


def extract_document_text(filename: str, data: bytes) -> str:
    # 统一入口：根据文件后缀决定用哪种抽取器。
    # 在 RAG 流水线里的位置：紧接在「下载 / 读取文件」之后、「切块」之前。
    # 输入：文件名（用于判断类型）、文件二进制内容。
    # 输出：一篇干净的纯文本（或 Markdown），交给下一步 chunk_document 切分。
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(data)    # PDF 走专用抽取
    if suffix == ".docx":
        return _extract_docx(data)   # Word 走专用抽取
    return decode_text(data)         # 其余当作纯文本解码
