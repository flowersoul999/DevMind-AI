"""DevMind AI module."""

from __future__ import annotations

import ast
import csv
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# 这两类后缀决定「文档型」文件的切块策略：Markdown 系 / PDF / Word 走结构感知切块；JSON / CSV 走结构化切块。
MARKDOWN_SUFFIXES = {".md", ".markdown", ".mdx", ".pdf", ".docx"}
STRUCTURED_DATA_SUFFIXES = {".json", ".csv"}


@dataclass
class _Block:
    # 内部用的「文本块」结构：一段连续文本 + 它在原文里的行号 / 类型 / 所属章节等位置信息。
    # 类比：像一本书里的一「段」，记录了它属于第几章、第几页、从哪行到哪行。
    text: str
    start_line: int | None
    end_line: int | None
    kind: str = "paragraph"  # 块类型：paragraph / heading / code_fence / json_value 等
    section_path: tuple[str, ...] = ()  # 所属章节路径，如 ("安装", "配置")
    section_instance: tuple[tuple[str, int], ...] = ()  # 章节+出现次数，区分同名章节
    page: int | None = None  # 来自 PDF 的页码（如有）
    metadata: dict[str, Any] = field(default_factory=dict)


MAX_CHUNKS_PER_DOCUMENT = 5000  # 单篇文档最多切 5000 块，防爆内存


def _validate_limits(max_chars: int, overlap: int) -> None:
    # 校验切块参数合法：每片最大字符必须 >0，重叠必须在 [0, max_chars) 之间。
    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    if overlap < 0 or overlap >= max_chars:
        raise ValueError("overlap must be between zero and max_chars")


def _normalize_text(text: str) -> str:
    # 统一换行符：把 \r\n 和 \r 都换成 \n，避免不同系统的换行差异影响按行处理。
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _stable_id(*parts: object) -> str:
    # 生成「稳定 id」：同样的输入永远得到同样的 24 位哈希，用于 chunk_id / parent_id 去重与回溯。
    # 为什么：让同一文档同一位置产出的碎片 id 可复现，便于增量更新时识别「是否同一块」。
    raw = "\x1f".join(str(part) for part in parts)  # 用控制字符 \x1f 做分隔（几乎不会出现在文本里）
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _line_for_offset(text: str, offset: int, first_line: int | None) -> int | None:
    # 把「字符偏移量」换算成「行号」：从 first_line 起，数到 offset 之前有多少个换行。
    if first_line is None:
        return None
    return first_line + text.count("\n", 0, max(0, offset))


def _split_long_text(block: _Block, max_chars: int) -> list[_Block]:
    # 把一个「超长文本块」按语义边界（句号 / 换行）切成不超过 max_chars 的小块。
    if len(block.text) <= max_chars:
        return [block]  # 没超长，原样返回

    # 优先按「句末标点 + 空白」或「换行」切分，保持句子完整
    units = [
        unit
        for unit in re.split(r"(?<=[。！？!?])|(?<=[.!?])\s+|\n+", block.text)
        if unit and unit.strip()
    ]
    if len(units) <= 1:
        # 整块都没有天然断句（比如一大串无标点文本）：直接按字符硬切
        units = [block.text[index : index + max_chars] for index in range(0, len(block.text), max_chars)]

    pieces: list[str] = []
    current = ""
    for unit in units:
        unit = unit.strip()
        if not unit:
            continue
        if len(unit) > max_chars:
            # 单句本身就超长：把当前累积先收尾，再对这句按字符切
            if current:
                pieces.append(current)
                current = ""
            pieces.extend(unit[index : index + max_chars] for index in range(0, len(unit), max_chars))
            continue
        candidate = f"{current}\n{unit}" if current else unit
        if current and len(candidate) > max_chars:
            # 加上这句会超长 → 先把当前收尾，这句作为下一段开头
            pieces.append(current)
            current = unit
        else:
            current = candidate
    if current:
        pieces.append(current)

    # 为切出来的每一片重新计算它在原文里的起止行号，并继承原块的位置元数据
    output: list[_Block] = []
    cursor = 0
    for piece in pieces:
        offset = block.text.find(piece, cursor)  # 在新块文本里找回这段在原文本的位置
        if offset < 0:
            offset = cursor  # 找不到（理论上不会）就退而用游标
        end_offset = min(len(block.text), offset + len(piece))
        output.append(
            _Block(
                text=piece,
                start_line=_line_for_offset(block.text, offset, block.start_line),
                end_line=_line_for_offset(block.text, max(offset, end_offset - 1), block.start_line),
                kind=block.kind,
                section_path=block.section_path,
                section_instance=block.section_instance,
                page=block.page,
                metadata=dict(block.metadata),
            )
        )
        cursor = end_offset
    return output


def _split_long_code_fence(block: _Block, max_chars: int) -> list[_Block]:
    # 专门处理「超长的代码围栏块（```...```）」：保持代码围栏标记完整，按行切，且不破坏语法上下文。
    if len(block.text) <= max_chars:
        return [block]  # 没超长
    lines = block.text.splitlines()
    if not lines:
        return []

    if max_chars <= 8:
        # 极端小的 max_chars：退化成普通超长文本切法，先把类型标成 code_fragment
        plain = _Block(
            text=block.text,
            start_line=block.start_line,
            end_line=block.end_line,
            kind="code_fragment",
            section_path=block.section_path,
            section_instance=block.section_instance,
            page=block.page,
            metadata=dict(block.metadata),
        )
        return _split_long_text(plain, max_chars)

    # 识别围栏的开闭合标记（``` 或 ~~~），并算上包裹它的标记占用的字符
    original_opener = lines[0] if lines[0].lstrip().startswith(("```", "~~~")) else "```"
    marker = original_opener.lstrip()[:3]
    opener = marker
    has_closer = len(lines) > 1 and lines[-1].lstrip().startswith(marker)
    body = lines[1:-1] if has_closer else lines[1:]
    closing = marker
    wrapper_size = len(opener) + len(closing) + 2  # 围栏标记 + 换行占用
    body_limit = max_chars - wrapper_size  # 真正能放代码的字符数

    if not body:
        return [
            _Block(
                text=f"{opener}\n{closing}",
                start_line=block.start_line,
                end_line=block.end_line,
                kind=block.kind,
                section_path=block.section_path,
                section_instance=block.section_instance,
                page=block.page,
                metadata=dict(block.metadata),
            )
        ]

    output: list[_Block] = []
    current: list[str] = []
    current_size = 0
    first_body_line = (block.start_line + 1) if block.start_line is not None else None
    current_start = first_body_line

    def flush(end_line: int | None) -> None:
        # 把累积的代码行打包成一个带围栏标记的块
        nonlocal current, current_size, current_start
        if not current:
            return
        body_text = "\n".join(current)
        output.append(
            _Block(
                text=f"{opener}\n{body_text}\n{closing}",
                start_line=current_start,
                end_line=end_line,
                kind=block.kind,
                section_path=block.section_path,
                section_instance=block.section_instance,
                page=block.page,
                metadata={
                    **block.metadata,
                    "fence_start_line": block.start_line,  # 记录整段代码原始起止行，便于回溯
                    "fence_end_line": block.end_line if has_closer else None,
                },
            )
        )
        current = []
        current_size = 0

    for body_index, line in enumerate(body):
        index = (first_body_line + body_index) if first_body_line is not None else None
        if len(line) > body_limit:
            # 单行就超长：先把之前的收尾，再把这一行按字符切（每行单独一个带围栏的块）
            flush((index - 1) if index is not None else None)
            for offset in range(0, len(line), body_limit):
                piece = line[offset : offset + body_limit]
                output.append(
                    _Block(
                        text=f"{opener}\n{piece}\n{closing}",
                        start_line=index,
                        end_line=index,
                        kind=block.kind,
                        section_path=block.section_path,
                        section_instance=block.section_instance,
                        page=block.page,
                        metadata={
                            **block.metadata,
                            "fence_start_line": block.start_line,
                            "fence_end_line": block.end_line if has_closer else None,
                        },
                    )
                )
            current_start = (index + 1) if index is not None else None
            continue
        line_size = len(line) + (1 if current else 0)  # +1 是行间换行
        if current and current_size + line_size > body_limit:
            # 再加这行会超 → 先收尾
            flush((index - 1) if index is not None else None)
            current_start = index
        if not current:
            current_start = index
        current.append(line)
        current_size += line_size
    last_body_line = None
    if block.end_line is not None:
        last_body_line = block.end_line - (1 if has_closer else 0)
    flush(last_body_line)
    return output


def _expand_block(block: _Block, max_chars: int) -> list[_Block]:
    # 根据块类型分派到「代码围栏」或「普通文本」的超长切分逻辑。
    if block.kind == "code_fence":
        return _split_long_code_fence(block, max_chars)
    return _split_long_text(block, max_chars)


def _overlap_blocks(blocks: list[_Block], overlap: int) -> list[_Block]:
    # 计算「片间重叠」：从上一片的尾部往前取 overlap 个字符，作为下一片的开头，让相邻片有上下文衔接。
    # 为什么：切片会切断语义，重叠一小段能让每片都带上「前情提要」，检索时不漏边界信息。
    if overlap <= 0 or not blocks:
        return []
    selected: list[_Block] = []
    remaining = overlap
    # 从后往前逐块取，凑够 overlap 个字符
    for block in reversed(blocks):
        if not block.text or block.kind == "code_fence":
            continue  # 空块或代码块不参与重叠（代码硬重叠会破坏语法）
        separator = 2 if selected else 0
        allowance = remaining - separator
        if allowance <= 0:
            break
        if len(block.text) <= allowance:
            selected.append(block)
            remaining -= len(block.text) + separator
            continue
        # 只取该块的尾部一段：尽量在句子 / 词边界截断
        offset = len(block.text) - allowance
        lookback_start = max(0, offset - min(64, max(16, allowance * 2)))
        lookback = block.text[lookback_start:offset]
        sentence_boundaries = list(re.finditer(r"[。！？.!?；;]\s*", lookback))
        word_boundaries = list(re.finditer(r"\s+", lookback))
        if sentence_boundaries:
            fragment_offset = lookback_start + sentence_boundaries[-1].end()  # 取最后一个句末
        elif word_boundaries:
            fragment_offset = lookback_start + word_boundaries[-1].end()      # 否则取最后一个空白
        else:
            # 都没有：在即将切入的窗口里找边界，实在没有就硬切
            boundary_window = block.text[offset : offset + min(48, max(8, allowance // 3))]
            boundary = re.search(r"\s+|[。！？.!?；;]\s*", boundary_window)
            fragment_offset = offset + boundary.end() if boundary else offset
        selected.append(
            _Block(
                text=block.text[fragment_offset:],
                start_line=_line_for_offset(block.text, fragment_offset, block.start_line),
                end_line=block.end_line,
                kind=block.kind,
                section_path=block.section_path,
                section_instance=block.section_instance,
                page=block.page,
                metadata={**block.metadata, "overlap_fragment": True},  # 标记这是重叠片段
            )
        )
        remaining = 0
        break
    return list(reversed(selected))


def _section_prefix(section_path: tuple[str, ...], page: int | None, limit: int | None = None) -> str:
    # 把章节路径渲染成 Markdown 标题前缀（如 "# 安装\n## 配置"），可附带页码，并裁剪长度。
    parts = [f"{'#' * min(index + 1, 6)} {title}" for index, title in enumerate(section_path)]  # 层级对应 # 个数，最多 6 级
    if page is not None:
        parts.append(f"Page {page}")  # 有页码就加一行
    value = "\n".join(parts)
    if limit is None or len(value) <= limit:
        return value
    if limit <= 3:
        return value[:limit]
    return value[: limit - 3].rstrip() + "..."  # 超长就截断并加省略号


def _prefix_for_chunk(
    section_path: tuple[str, ...],
    page: int | None,
    max_chars: int,
    *,
    has_body: bool,
) -> str:
    # 根据是否有正文、max_chars 大小，算一个合理的「章节前缀长度上限」。
    if not section_path and page is None:
        return ""  # 既不是章节也不是带页码 → 无前缀
    if not has_body:
        limit = max_chars
    elif max_chars <= 3:
        limit = 0
    else:
        # 前缀最多占 min(512, 1/3 片长, 片长-3)，避免前缀挤掉正文
        limit = min(512, max_chars // 3, max_chars - 3)
    return _section_prefix(section_path, page, limit=limit) if limit > 0 else ""


def _join_block_text(blocks: list[_Block]) -> str:
    # 把若干块的正文用空行拼起来（跳过空块），形成一段连续文本。
    return "\n\n".join(block.text for block in blocks if block.text).strip()


def _emit_chunk(
    blocks: list[_Block],
    *,
    source_path: str,
    chunk_type: str,
    child_index: int,
    max_chars: int,
) -> dict[str, Any]:
    # 把一组相邻块「发射」成一个最终碎片字典：拼正文 + 加章节前缀 + 算各种 id / 行号 / 元数据。
    first = blocks[0]
    body = _join_block_text(blocks)
    prefix = _prefix_for_chunk(first.section_path, first.page, max_chars, has_body=bool(body))
    content = f"{prefix}\n\n{body}" if prefix and body else (prefix or body)  # 有前缀就前缀+正文，否则取其一
    parent_id = _stable_id(
        "parent",
        source_path,
        first.section_instance or tuple((title, 0) for title in first.section_path),  # 没有实例信息时退化成 (标题,0)
        first.page,
    )
    # 若这组块里含代码围栏，碎片类型标成 markdown_code（便于检索时区分）
    effective_type = "markdown_code" if any(block.kind == "code_fence" for block in blocks) else chunk_type
    metadata: dict[str, Any] = {}
    for block in blocks:
        metadata.update(block.metadata)  # 合并所有块自带的元数据
    return {
        "content": content,
        "chunk_id": _stable_id("chunk", parent_id, child_index, content),  # 碎片唯一 id
        "parent_id": parent_id,  # 所属父片段 id（同父碎片共享）
        "child_index": child_index,  # 父片段下的第几块
        "chunk_type": effective_type,
        "section_path": list(first.section_path),
        "section_title": first.section_path[-1] if first.section_path else None,
        "page": first.page,
        "start_line": min((block.start_line for block in blocks if block.start_line is not None), default=None),
        "end_line": max((block.end_line for block in blocks if block.end_line is not None), default=None),
        **metadata,
    }


def _pack_blocks(
    blocks: list[_Block],
    *,
    source_path: str,
    max_chars: int,
    overlap: int,
    chunk_type: str,
) -> list[dict[str, Any]]:
    # 把「块列表」打包成最终碎片：按章节 / 页码分组，超长块先展开，再在 max_chars 内尽量拼满，并加重叠。
    chunks: list[dict[str, Any]] = []
    current: list[_Block] = []
    current_key: tuple[tuple[tuple[str, int], ...], int | None] | None = None
    child_counts: dict[tuple[tuple[tuple[str, int], ...], int | None], int] = {}

    def flush() -> None:
        # 把当前累积的一组块发射成一个碎片
        nonlocal current
        if not current or current_key is None:
            return
        child_index = child_counts.get(current_key, 0)
        chunks.append(
            _emit_chunk(
                current,
                source_path=source_path,
                chunk_type=chunk_type,
                child_index=child_index,
                max_chars=max_chars,
            )
        )
        child_counts[current_key] = child_index + 1

    for raw_block in blocks:
        key = (raw_block.section_instance, raw_block.page)
        prefix = _prefix_for_chunk(
            raw_block.section_path,
            raw_block.page,
            max_chars,
            has_body=bool(raw_block.text),
        )
        prefix_size = len(prefix)
        body_limit = max(1, max_chars - prefix_size - (2 if prefix_size else 0))  # 留给正文的空间
        for block in _expand_block(raw_block, body_limit):
            key = (block.section_instance, block.page)
            if current and key != current_key:
                # 章节 / 页码变了 → 先发射当前碎片，重新起一组
                flush()
                current = []
            current_key = key
            candidate_body = _join_block_text([*current, block])
            prefix = _prefix_for_chunk(
                block.section_path,
                block.page,
                max_chars,
                has_body=bool(candidate_body),
            )
            candidate_size = len(candidate_body) + len(prefix) + (2 if prefix else 0)
            if current and candidate_size > max_chars:
                # 加这块会超长：先把当前发射，并让下一组从「重叠片段」开始
                previous = list(current)
                flush()
                current = _overlap_blocks(previous, overlap)
                while current:
                    candidate_body = _join_block_text([*current, block])
                    if len(candidate_body) + len(prefix) + (2 if prefix else 0) <= max_chars:
                        break
                    current.pop(0)  # 仍超长就从前往后丢，直到装得下
            current.append(block)
    flush()
    # 最后校验：任何碎片内容都不应超过 max_chars（理论上不会，这里做防御）
    if any(len(chunk["content"]) > max_chars for chunk in chunks):
        raise AssertionError("chunk content exceeded max_chars")
    return chunks


def _plain_blocks(text: str) -> list[_Block]:
    # 把纯文本按「空行分段」切成块：相邻非空行算一段，记录起止行号。
    lines = text.splitlines()
    blocks: list[_Block] = []
    current: list[str] = []
    start_line = 1

    def flush(end_line: int) -> None:
        # 把累积的行打包成一个 paragraph 块
        nonlocal current
        content = "\n".join(current).strip()
        if content:
            blocks.append(_Block(content, start_line, max(start_line, end_line)))
        current = []

    for line_number, line in enumerate(lines, start=1):
        if not line.strip():
            flush(line_number - 1)  # 遇到空行 → 段结束
            continue
        if not current:
            start_line = line_number  # 新段起点
        current.append(line)
    flush(len(lines))
    return blocks


def _plain_chunks(
    text: str,
    *,
    source_path: str,
    max_chars: int,
    overlap: int,
) -> list[dict[str, Any]]:
    # 「无结构纯文本」的切块：按 max_chars 滑动窗口切，尽量在空行 / 换行处断开，并加重叠。
    chunks: list[dict[str, Any]] = []
    parent_id = _stable_id("parent", source_path, "plain")
    cursor = 0
    text_length = len(text)
    while cursor < text_length:
        end = min(text_length, cursor + max_chars)
        if end < text_length:
            # 尽量在窗口内找「后半段」的空行 / 换行作为切断点，保持段落完整
            search_start = cursor + max(1, max_chars // 2)
            candidates = [
                text.rfind("\n\n", search_start, end),
                text.rfind("\n", search_start, end),
            ]
            boundary = max(candidates)
            if boundary > cursor:
                end = boundary + (2 if text.startswith("\n\n", boundary) else 1)
        raw = text[cursor:end]
        left_trim = len(raw) - len(raw.lstrip())  # 去掉左空白的长度
        right_trim = len(raw.rstrip())            # 去掉右空白后的长度
        content = raw[left_trim:right_trim]
        if content:
            start_offset = cursor + left_trim
            end_offset = cursor + right_trim
            child_index = len(chunks)
            chunks.append(
                {
                    "content": content,
                    "chunk_id": _stable_id("chunk", parent_id, child_index, content),
                    "parent_id": parent_id,
                    "child_index": child_index,
                    "chunk_type": "text",
                    "section_path": [],
                    "section_title": None,
                    "page": None,
                    "start_line": _line_for_offset(text, start_offset, 1),
                    "end_line": _line_for_offset(text, max(start_offset, end_offset - 1), 1),
                }
            )
            if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
                raise ValueError(f"document exceeds the {MAX_CHUNKS_PER_DOCUMENT} chunk limit")
        if end >= text_length:
            break
        cursor = max(cursor + 1, end - overlap)  # 下一窗口起点：回退 overlap 做重叠
    return chunks


def _markdown_blocks(text: str) -> list[_Block]:
    # 「Markdown / PDF 抽取文本」按结构切成块：识别标题层级、代码围栏、分页标记，生成带章节路径的块。
    # 为什么：结构感知切块能保留「属于哪一章哪一节」的语义，比盲目按字数切要好得多。
    lines = text.splitlines()
    blocks: list[_Block] = []
    headings: list[tuple[int, str, int]] = []  # (级别, 标题, 出现次数)
    heading_occurrences: dict[tuple[tuple[tuple[str, int], ...], int, str], int] = {}  # 同名章节计数，区分重复标题
    current_page: int | None = None
    current: list[str] = []
    current_start = 1
    current_kind = "paragraph"
    fence_marker: str | None = None  # 当前代码围栏的标记（``` 或 ~~~）
    heading_re = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")  # 匹配 Markdown 标题
    page_re = re.compile(r"^<!--\s*page\s*:\s*(\d+)\s*-->$", re.IGNORECASE)  # 匹配 PDF 抽出的分页注释

    def section_path() -> tuple[str, ...]:
        # 当前章节路径 = 各级标题文本
        return tuple(title for _, title, _ in headings)

    def section_instance() -> tuple[tuple[str, int], ...]:
        # 当前章节「实例」= (标题, 出现次数)，区分同名不同位置的章节
        return tuple((title, occurrence) for _, title, occurrence in headings)

    def flush(end_line: int) -> None:
        # 把当前累积的行打包成一个块（paragraph / code_fence / heading）
        nonlocal current
        content = "\n".join(current).strip()
        if content:
            blocks.append(
                _Block(
                    content,
                    current_start,
                    max(current_start, end_line),
                    kind=current_kind,
                    section_path=section_path(),
                    section_instance=section_instance(),
                    page=current_page,
                )
            )
        current = []

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if fence_marker is not None:
            # 正在代码围栏内部：继续收集，直到遇到闭合标记
            current.append(line)
            if stripped.startswith(fence_marker):
                flush(line_number)  # 闭合 → 发射 code_fence 块
                fence_marker = None
                current_kind = "paragraph"
            continue

        page_match = page_re.match(stripped)
        if page_match:
            # 分页标记：先发射已累积的块，再更新当前页码
            flush(line_number - 1)
            current_page = int(page_match.group(1))
            continue

        heading_match = heading_re.match(line)
        if heading_match:
            # 标题行：先发射之前的内容块，再调整标题栈
            flush(line_number - 1)
            level = len(heading_match.group(1))  # # 个数 = 层级
            title = heading_match.group(2).strip()
            while headings and headings[-1][0] >= level:
                headings.pop()  # 弹出同级或更深的旧标题，维护层级
            parent_instance = section_instance()
            occurrence_key = (parent_instance, level, title)
            occurrence = heading_occurrences.get(occurrence_key, 0)
            heading_occurrences[occurrence_key] = occurrence + 1
            headings.append((level, title, occurrence))
            # 标题本身也作为一个 heading 块（空文本，仅记录位置）
            blocks.append(
                _Block(
                    "",
                    line_number,
                    line_number,
                    kind="heading",
                    section_path=section_path(),
                    section_instance=section_instance(),
                    page=current_page,
                    metadata={"heading_line": line_number},
                )
            )
            continue

        if stripped.startswith(("```", "~~~")):
            # 代码围栏开始：发射之前内容，进入 code_fence 收集模式
            flush(line_number - 1)
            current_start = line_number
            current_kind = "code_fence"
            fence_marker = stripped[:3]
            current = [line]
            continue

        if not stripped:
            flush(line_number - 1)  # 空行 → 段落结束
            continue
        if not current:
            current_start = line_number
        current.append(line)
    flush(len(lines))
    return blocks


def _json_blocks(text: str, max_chars: int) -> list[_Block]:
    # 「JSON」结构化切块：整体不超长就整段一块；超长就按顶层 key / 数组元素各自成块。
    try:
        value = json.loads(text)  # 先尝试解析
    except (TypeError, ValueError, json.JSONDecodeError):
        return _plain_blocks(text)  # 解析失败 → 退化成纯文本切块

    formatted = json.dumps(value, ensure_ascii=False, indent=2)  # 美化输出，便于阅读
    if len(formatted) <= max_chars:
        return [
            _Block(
                formatted,
                1,
                max(1, len(text.splitlines())),
                kind="json_value",
                metadata={"json_path": "$"},  # 整份 JSON 的路径
            )
        ]

    blocks: list[_Block] = []
    if isinstance(value, dict):
        # 字典：每个顶层 key 一块，路径写成 $.key
        items = [(f"$.{key}", {key: item}) for key, item in value.items()]
    elif isinstance(value, list):
        # 列表：每个元素一块，路径写成 $[i]
        items = [(f"$[{index}]", item) for index, item in enumerate(value)]
    else:
        items = [("$", value)]
    for json_path, item in items:
        blocks.append(
            _Block(
                json.dumps(item, ensure_ascii=False, indent=2),
                None,
                None,
                kind="json_value",
                section_path=(json_path,),
                section_instance=((json_path, 0),),
                metadata={"json_path": json_path},
            )
        )
    return blocks


def _csv_chunks(text: str, source_path: str, max_chars: int, overlap: int) -> list[dict[str, Any]]:
    # 「CSV」结构化切块：保留表头，按行分批拼成碎片，超长行再按字符切；支持重叠与超大字段。
    try:
        if len(text) > csv.field_size_limit():
            csv.field_size_limit(len(text))  # 超大字段时放宽 csv 解析上限
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error:
        # CSV 解析失败 → 退化成纯文本切块
        return _pack_blocks(
            _plain_blocks(text),
            source_path=source_path,
            max_chars=max_chars,
            overlap=overlap,
            chunk_type="text",
        )
    if not rows:
        return []

    def render(row: list[str]) -> str:
        # 把一行渲染回 CSV 文本（无换行符）
        buffer = io.StringIO()
        csv.writer(buffer, lineterminator="").writerow(row)
        return buffer.getvalue()

    header = render(rows[0])  # 表头单独处理，每个碎片都带上
    chunks: list[dict[str, Any]] = []
    parent_id = _stable_id("parent", source_path, "csv")

    def append_chunk(
        content: str,
        *,
        row_start: int,
        row_end: int,
        chunk_type: str = "csv_rows",
        row_part: int | None = None,
        row_parts: int | None = None,
        precise_lines: bool = True,
    ) -> None:
        # 发出一个 CSV 碎片（含表头 + 若干数据行）
        child_index = len(chunks)
        chunks.append(
            {
                "content": content,
                "chunk_id": _stable_id("chunk", parent_id, child_index, content),
                "parent_id": parent_id,
                "child_index": child_index,
                "chunk_type": chunk_type,
                "section_path": [],
                "section_title": None,
                "page": None,
                "start_line": row_start if precise_lines else None,
                "end_line": row_end if precise_lines else None,
                "row_start": row_start,
                "row_end": row_end,
                **({"row_part": row_part, "row_parts": row_parts} if row_part is not None else {}),
            }
        )
        if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
            raise ValueError(f"document exceeds the {MAX_CHUNKS_PER_DOCUMENT} chunk limit")

    if header and len(header) + 1 >= max_chars:
        # 表头本身就超长：把表头按字符切块
        header_parts = [header[index : index + max_chars] for index in range(0, len(header), max_chars)] or [""]
        for part_index, part in enumerate(header_parts):
            append_chunk(
                part,
                row_start=1,
                row_end=1,
                chunk_type="csv_header_part",
                row_part=part_index + 1,
                row_parts=len(header_parts),
                precise_lines="\n" not in part,
            )
        header_prefix = ""
    else:
        header_prefix = header  # 表头作为每个碎片的前缀

    if not rows[1:]:
        # 没有数据行（只有表头）：如果还没发出任何碎片，就把表头单独发一个
        if not chunks:
            append_chunk(header, row_start=1, row_end=1, precise_lines="\n" not in header)
        return chunks

    # 先算出每行（及超长行的子段）的「渲染片段」，记录它在第几行、是不是某行的第几段
    prefix_size = len(header_prefix) + (1 if header_prefix else 0)
    row_budget = max_chars - prefix_size  # 数据部分可用字符
    records: list[tuple[str, int, int | None, int | None, bool]] = []
    for row_number, row in enumerate(rows[1:], start=2):
        rendered = render(row)
        parts = [rendered[index : index + row_budget] for index in range(0, len(rendered), row_budget)] or [""]
        for part_index, part in enumerate(parts):
            records.append((part, row_number, part_index + 1 if len(parts) > 1 else None, len(parts) if len(parts) > 1 else None, "\n" not in rendered))
    # 贪心地把相邻行拼进同一个碎片，直到再加一行会超 max_chars
    start = 0
    while start < len(records):
        selected: list[tuple[str, int, int | None, int | None, bool]] = []
        index = start
        while index < len(records):
            candidate_rows = [item[0] for item in selected]
            candidate_rows.append(records[index][0])
            candidate = "\n".join([header_prefix, *candidate_rows]) if header_prefix else "\n".join(candidate_rows)
            if selected and len(candidate) > max_chars:
                break  # 再加一行就超 → 停止本碎片
            selected.append(records[index])
            index += 1
        selected_text = [item[0] for item in selected]
        content = "\n".join([header_prefix, *selected_text]) if header_prefix else "\n".join(selected_text)
        row_start = min(item[1] for item in selected)
        row_end = max(item[1] for item in selected)
        row_part = selected[0][2] if len(selected) == 1 else None
        row_parts = selected[0][3] if len(selected) == 1 else None
        append_chunk(
            content,
            row_start=row_start,
            row_end=row_end,
            row_part=row_part,
            row_parts=row_parts,
            precise_lines=all(item[4] for item in selected),
        )
        if index >= len(records):
            break
        if overlap <= 0:
            start = index
            continue
        # 重叠：从本碎片尾部往回取若干行补到下个碎片开头
        size = 0
        overlap_records = 0
        for record in reversed(selected):
            record_size = len(record[0])
            if overlap_records and size + record_size > overlap:
                break
            size += record_size
            overlap_records += 1
            if size >= overlap:
                break
        start = max(start + 1, index - overlap_records)
    if any(len(chunk["content"]) > max_chars for chunk in chunks):
        raise AssertionError("CSV chunk content exceeded max_chars")
    return chunks


def chunk_document(
    text: str,
    source_path: str = "document.txt",
    max_chars: int = 1600,
    overlap: int = 160,
) -> list[dict[str, Any]]:
    """Split a document on structural boundaries and return retrieval metadata."""
    # 文档切块主入口：根据文件后缀选择对应的切块策略（CSV / Markdown 系 / JSON / 纯文本）。
    # 在 RAG 流水线里的位置：抽取文本之后、向量化之前——把长文切成适合检索的「小片段」。
    # 输入：原文文本、来源路径（决定类型）、每片最大字符、片间重叠。
    # 输出：碎片字典列表（每片含 content、chunk_id、行列号、章节、元数据等）。

    _validate_limits(max_chars, overlap)  # 先校验参数
    if not text:
        return []
    normalized = _normalize_text(text)  # 统一换行
    if not normalized.strip():
        return []

    suffix = Path(source_path).suffix.lower()
    if suffix == ".csv":
        return _csv_chunks(normalized, source_path, max_chars, overlap)  # CSV 专用
    if suffix in MARKDOWN_SUFFIXES:
        blocks = _markdown_blocks(normalized)  # Markdown / PDF / Word 抽出文本 → 结构切块
        chunk_type = "markdown_section"
    elif suffix == ".json":
        blocks = _json_blocks(normalized, max_chars)  # JSON 结构化切块
        chunk_type = "json_value"
    else:
        # 其它（.txt / .log 等）走纯文本切块
        return _plain_chunks(
            normalized,
            source_path=source_path,
            max_chars=max_chars,
            overlap=overlap,
        )
    chunks = _pack_blocks(
        blocks,
        source_path=source_path,
        max_chars=max_chars,
        overlap=overlap,
        chunk_type=chunk_type,
    )
    if len(chunks) > MAX_CHUNKS_PER_DOCUMENT:
        raise ValueError(f"document exceeds the {MAX_CHUNKS_PER_DOCUMENT} chunk limit")
    return chunks


def chunk_text(text: str, max_chars: int = 1600, overlap: int = 160) -> list[str]:
    """Backward-compatible plain-text API used by older indexing paths."""
    # 老接口兼容：只返回各碎片的 content 字符串列表，给旧索引路径用。
    return [
        chunk["content"]
        for chunk in chunk_document(text, "document.txt", max_chars=max_chars, overlap=overlap)
    ]


def _line_windows(
    lines: list[str],
    start_line: int,
    end_line: int,
    max_chars: int,
    overlap_lines: int = 6,
) -> list[tuple[str, int, int]]:
    # 按「行窗口」把一段代码切成不超过 max_chars 的小段，窗口之间重叠 overlap_lines 行。
    output: list[tuple[str, int, int]] = []
    cursor = max(0, start_line - 1)
    stop = min(len(lines), end_line)
    while cursor < stop:
        if len(lines[cursor]) > max_chars:
            # 单行就超长：这行单独按字符切
            for offset in range(0, len(lines[cursor]), max_chars):
                output.append((lines[cursor][offset : offset + max_chars], cursor + 1, cursor + 1))
            cursor += 1
            continue
        size = 0
        end = cursor
        while end < stop:
            line_size = len(lines[end]) + (1 if end > cursor else 0)
            if end > cursor and size + line_size > max_chars:
                break  # 再加一行超了 → 停
            size += line_size
            end += 1
        if end > cursor:
            content = "\n".join(lines[cursor:end]).strip("\n")
            if content:
                output.append((content, cursor + 1, end))  # (内容, 起始行, 结束行)
        if end >= stop:
            break
        cursor = max(cursor + 1, end - overlap_lines)  # 回退 overlap_lines 做重叠
    return output


def _python_regions(text: str, max_chars: int) -> list[tuple[int, int, str, str]]:
    # 用 Python 的 ast 解析源码，按「顶层函数 / 类 / 方法」切分代码区域；超长的类会进一步拆成方法 / 类头。
    try:
        tree = ast.parse(text)  # 语法解析；语法错误就返回空（走通用回退）
    except (SyntaxError, ValueError, RecursionError):
        return []
    lines = text.splitlines()
    regions: list[tuple[int, int, str, str]] = []
    cursor = 1

    def node_start(node: ast.AST) -> int:
        # 节点起始行：取节点本身和它的装饰器里最小的行号
        decorators = getattr(node, "decorator_list", []) or []
        positions = [getattr(node, "lineno", 1), *(getattr(item, "lineno", 1) for item in decorators)]
        return min(positions)

    def append_node(node: ast.AST, parent: str | None = None) -> None:
        # 把一个函数 / 类节点登记为一个 region（区域）
        start = node_start(node)
        end = int(getattr(node, "end_lineno", start) or start)
        name = str(getattr(node, "name", "module"))
        symbol = f"{parent}.{name}" if parent else name
        kind = "class" if isinstance(node, ast.ClassDef) else "function"
        if isinstance(node, ast.ClassDef) and len("\n".join(lines[start - 1 : end])) > max_chars:
            # 类整体超长：拆成「类头」+ 每个「方法」各自成区域
            methods = [
                item
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            ]
            if methods:
                method_cursor = start
                first_method_start = node_start(methods[0])
                if first_method_start > method_cursor:
                    regions.append((method_cursor, first_method_start - 1, symbol, "class_header"))  # 类头（到第一个方法之前）
                for index, method in enumerate(methods):
                    method_start = node_start(method)
                    next_start = node_start(methods[index + 1]) if index + 1 < len(methods) else end + 1
                    regions.append((method_start, next_start - 1, f"{symbol}.{method.name}", "method"))  # 单个方法
                return
        regions.append((start, end, symbol, kind))

    # 只处理顶层定义（函数 / 异步函数 / 类），它们之间的「模块级」代码也作为 module 区域
    top_level = [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
    for node in top_level:
        start = node_start(node)
        end = int(getattr(node, "end_lineno", start) or start)
        if start > cursor:
            regions.append((cursor, start - 1, "module", "module"))  # 顶层定义之间的杂项代码
        append_node(node)
        cursor = max(cursor, end + 1)
    if cursor <= len(lines):
        regions.append((cursor, len(lines), "module", "module"))  # 末尾剩余模块代码
    return [region for region in regions if region[0] <= region[1]]  # 过滤掉起止颠倒的


def _generic_symbol(line: str, suffix: str) -> tuple[str, str] | None:
    # 对非 Python 语言，用正则识别一行里的「符号声明」（函数 / 类 / 接口 / 变量等）及其类型。
    patterns: list[tuple[re.Pattern[str], str]] = []
    if suffix in {".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}:
        patterns = [
            (
                re.compile(
                    r"^\s*(?:export\s+(?:default\s+)?)?(?:(?:async\s+)?function|class|interface|type|enum)\s+([A-Za-z_$][\w$]*)"
                ),
                "symbol",
            ),
            (
                re.compile(
                    r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?(?:function\b|\([^)]*\)\s*=>|[A-Za-z_$][\w$]*\s*=>)"
                ),
                "function",
            ),
        ]
    elif suffix == ".go":
        patterns = [(re.compile(r"^\s*(?:func\s+(?:\([^)]*\)\s*)?|type\s+)([A-Za-z_]\w*)"), "symbol")]
    elif suffix == ".rs":
        patterns = [
            (re.compile(r"^\s*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl)\s+([A-Za-z_]\w*)"), "symbol")
        ]
    elif suffix in {".java", ".kt", ".kts", ".cs", ".cpp", ".c", ".h", ".hpp", ".php", ".rb", ".swift", ".scala"}:
        patterns = [
            (re.compile(r"^\s*(?:(?:public|private|protected|internal|static|final|abstract|sealed|data|open)\s+)*(?:class|interface|enum|struct|trait|record)\s+([A-Za-z_]\w*)"), "type")
        ]
    elif suffix == ".sql":
        patterns = [(re.compile(r"^\s*CREATE\s+(?:OR\s+REPLACE\s+)?(?:FUNCTION|PROCEDURE|TABLE|VIEW)\s+([\w.]+)", re.IGNORECASE), "sql_object")]
    elif suffix in {".sh", ".bash", ".zsh", ".ps1"}:
        patterns = [(re.compile(r"^\s*(?:function\s+)?([A-Za-z_]\w*)\s*(?:\(\))?\s*\{"), "function")]
    for pattern, kind in patterns:
        match = pattern.match(line)
        if match:
            return match.group(1), kind  # (符号名, 类型)
    return None


def _generic_regions(text: str, suffix: str) -> list[tuple[int, int, str, str]]:
    # 对通用语言：扫描每行找符号声明，把相邻声明之间的代码也补成 module 区域。
    lines = text.splitlines()
    starts: list[tuple[int, str, str]] = []
    for line_number, line in enumerate(lines, start=1):
        symbol = _generic_symbol(line, suffix)
        if symbol:
            starts.append((line_number, symbol[0], symbol[1]))
    if not starts:
        return [(1, len(lines), "module", "module")] if lines else []  # 没识别到符号就整段当 module

    regions: list[tuple[int, int, str, str]] = []
    if starts[0][0] > 1:
        regions.append((1, starts[0][0] - 1, "module", "module"))  # 第一个符号之前的代码
    for index, (start, name, kind) in enumerate(starts):
        end = starts[index + 1][0] - 1 if index + 1 < len(starts) else len(lines)
        regions.append((start, end, name, kind))  # 每个符号到下一个符号之间是一段
    return regions


def _language_for_path(path: str) -> str:
    # 取文件后缀（不含点）作为「语言」标识；没有后缀就用文件名。
    suffix = Path(path).suffix.lower().lstrip(".")
    return suffix or Path(path).name.lower()


def chunk_code_text(text: str, path: str, max_chars: int = 2200) -> list[dict[str, Any]]:
    """Split source on language symbols, with parser and line-window fallbacks."""
    # 代码切块主入口：按语言把源码切成以「符号（函数 / 类 / 方法）」为单位的碎片，并附文件 / 行号 / 语言元数据。
    # 在 RAG 流水线里的位置：源码虽不进向量库（走工作区实时搜索），但知识库里的代码文件仍可用此切法建索引。
    # 输入：源码文本、文件路径（决定语言）、每片最大字符。
    # 输出：碎片字典列表，含 symbol / language / header 等便于定位的信息。

    if max_chars <= 0:
        raise ValueError("max_chars must be greater than zero")
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")  # 统一换行
    if not normalized.strip():
        return []
    suffix = Path(path).suffix.lower()
    language = _language_for_path(path)

    if suffix in MARKDOWN_SUFFIXES | STRUCTURED_DATA_SUFFIXES | {".txt"}:
        # 若「代码文件」其实是文档 / 结构化数据，退化成文档切块，并补上文件头信息
        document_chunks = chunk_document(normalized, path, max_chars=max_chars, overlap=max(0, min(160, max_chars // 10)))
        output: list[dict[str, Any]] = []
        for chunk in document_chunks:
            symbol = chunk.get("section_title") or "document"
            header = f"File: {path}\nSection: {symbol}\nLines: {chunk['start_line']}-{chunk['end_line']}"
            output.append({**chunk, "symbol": symbol, "symbol_kind": chunk["chunk_type"], "language": language, "header": header})
        return output

    # 真正的代码：Python 用 ast 解析，其它语言用正则识别符号
    lines = normalized.splitlines()
    regions = _python_regions(normalized, max_chars) if suffix == ".py" else _generic_regions(normalized, suffix)
    if not regions:
        regions = [(1, len(lines), "module", "module")]  # 兜底

    chunks: list[dict[str, Any]] = []
    symbol_occurrences: dict[str, int] = {}
    for start_line, end_line, symbol, symbol_kind in regions:
        symbol_occurrence = symbol_occurrences.get(symbol, 0)
        symbol_occurrences[symbol] = symbol_occurrence + 1
        parent_id = _stable_id("code-parent", path, symbol, symbol_occurrence)
        for part_index, (content, part_start, part_end) in enumerate(
            _line_windows(lines, start_line, end_line, max_chars=max_chars)
        ):
            if not content.strip():
                continue
            chunk_id = _stable_id("code", parent_id, part_index, content)
            part_suffix = f" (part {part_index + 1})" if part_index else ""
            chunks.append(
                {
                    "content": content,
                    "start_line": part_start,
                    "end_line": part_end,
                    "symbol": symbol,
                    "symbol_kind": symbol_kind,
                    "language": language,
                    "chunk_type": "code_symbol" if symbol != "module" else "code_module",
                    "chunk_id": chunk_id,
                    "parent_id": parent_id,
                    "child_index": part_index,
                    "symbol_occurrence": symbol_occurrence,
                    "header": (
                        f"File: {path}\nLanguage: {language}\nSymbol: {symbol}{part_suffix}\n"
                        f"Lines: {part_start}-{part_end}"
                    ),
                }
            )
    return chunks


def chunk_pr_files(files: list[dict[str, Any]], max_chars: int = 1800) -> list[dict[str, Any]]:
    # 把 PR 的 diff 补丁（patch）切块：每个文件一个 chunk 组，超长补丁再按 max_chars 切分（overlap=0）。
    # 为什么：PR 补丁是「变更内容」，需要按文件检索，方便回答「某次 PR 改了什么」。
    chunks: list[dict[str, Any]] = []
    for file in files:
        patch = file.get("patch") or ""
        filename = str(file.get("filename") or "patch.diff")
        for index, content in enumerate(chunk_text(patch, max_chars=max_chars, overlap=0)):
            chunks.append(
                {
                    "filename": filename,
                    "chunk_index": index,
                    "chunk_id": _stable_id("patch", filename, content),
                    "content": content,
                }
            )
    return chunks
