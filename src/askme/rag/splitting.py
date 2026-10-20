"""按 Markdown 标题与结构分块，使用真实 BGE tokenizer 计量长度。"""

import hashlib
import re
from functools import lru_cache
from html.parser import HTMLParser
from pathlib import Path

from huggingface_hub import hf_hub_download
from langchain_text_splitters import RecursiveCharacterTextSplitter
from markdown_it import MarkdownIt
from tokenizers import Tokenizer

from askme.core.config import PROJECT_ROOT, Settings
from askme.core.errors import AppError


@lru_cache(maxsize=4)
def load_tokenizer(location: str, revision: str, local_only: bool) -> Tokenizer:
    try:
        path = Path(location)
        if path.is_dir():
            path = path / "tokenizer.json"
        if not path.is_file():
            path = Path(hf_hub_download(repo_id=location, filename="tokenizer.json", revision=revision, local_files_only=local_only))
        tokenizer = Tokenizer.from_file(str(path))
        tokenizer.no_truncation()
        tokenizer.no_padding()
        return tokenizer
    except Exception:
        raise AppError("TOKENIZER_UNAVAILABLE", "无法加载 BGE-M3 tokenizer，请配置本地 tokenizer.json；如需下载，请按运行文档单独准备", 503) from None


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.hidden += 1
        if not self.hidden and tag == "img":
            alt = dict(attrs).get("alt", "")
            if alt:
                self.parts.append(f"\n[图片说明] {alt}\n")
        elif tag in {"td", "th"}:
            self.parts.append(" | ")
        elif tag in {"p", "div", "br", "tr", "li"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag in {"p", "div", "tr", "li"}:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def retrieval_text(markdown: str) -> str:
    # 仅替换 HTML 标签并提取 img.alt；普通 Markdown、代码和表格保持可读结构。
    def replace_html(match):
        reader = PlainText()
        reader.feed(match.group())
        return "".join(reader.parts)
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", markdown, flags=re.S | re.I)
    text = re.sub(r"<img\b[^>]*>", replace_html, text, flags=re.I)
    text = re.sub(r"!\[([^\]]*)\]\([^\n]*?\)", lambda match: f"[图片说明] {match[1]}" if match[1] else "", text)
    return text.strip()


def split_markdown(markdown: str, settings: Settings, task: dict) -> list[dict]:
    location = Path(settings.tokenizer_path)
    if location.suffix == ".json" or (PROJECT_ROOT / location).exists():
        location = PROJECT_ROOT / location
    tokenizer = load_tokenizer(str(location), settings.embedding_revision, settings.tokenizer_local_only)

    def length(text: str) -> int:
        return len(tokenizer.encode(text, add_special_tokens=True).ids)

    tokens = MarkdownIt("commonmark", {"html": True}).enable("table").parse(markdown)
    lines = markdown.splitlines(keepends=True)
    headings, sections, current = [], [], None
    for token in tokens:
        if token.level != 0 or token.map is None or token.type.endswith("_close") or token.type == "hr":
            continue
        start, end = token.map
        if token.type == "heading_open":
            title = " ".join(lines[start:end]).strip()
            title = re.sub(r"^#{1,6}\s*", "", title.splitlines()[0]).strip(" #")
            level = int(token.tag[1:])
            while headings and headings[-1][0] >= level:
                headings.pop()
            headings.append((level, title))
            current = {"path": [value for _, value in headings], "blocks": []}
            sections.append(current)
            continue
        if current is None:
            current = {"path": [], "blocks": []}
            sections.append(current)
        body = "".join(lines[start:end]).strip()
        if body:
            current["blocks"].append({"text": body, "lines": [start + 1, end], "kind": token.type})

    parts = []
    for section in sections:
        path = section["path"]
        prefix = " > ".join(path)
        if prefix and length(prefix) > settings.chunk_max_size // 2:
            raise AppError("HEADING_TOO_LONG", "标题层级文本过长，请简化标题后重新导入", 422)
        budget = max(16, settings.chunk_size - length(prefix + "\n\n"))
        splitter = RecursiveCharacterTextSplitter(chunk_size=budget,
            chunk_overlap=min(settings.chunk_overlap, budget // 3), length_function=length,
            separators=["\n\n", "\n", "。", "！", "？", ". ", "; ", " ", ""], keep_separator=True)
        for block in section["blocks"]:
            body = block["text"] if block["kind"] in {"fence", "code_block"} else retrieval_text(block["text"])
            if block["kind"] == "html_block":
                reader = PlainText()
                reader.feed(body)
                body = "".join(reader.parts).strip()
            if not body:
                continue
            atomic = block["kind"] in {"fence", "code_block", "table_open", "html_block"}
            rendered = f"{prefix}\n\n{body}".strip()
            fragments = [body] if length(rendered) <= settings.chunk_size or (atomic and length(rendered) <= settings.chunk_max_size) else splitter.split_text(body)
            if len(fragments) > 1 and block["kind"] == "fence":
                rows = body.splitlines()
                opening = rows[0]
                marker = re.match(r"(`{3,}|~{3,})", opening)
                closing = marker.group() if marker else "```"
                code = "\n".join(rows[1:-1]) if rows[-1].strip().startswith(closing) else "\n".join(rows[1:])
                code_budget = budget - length(opening + "\n" + closing)
                if code_budget < 8:
                    raise AppError("CODE_HEADER_TOO_LONG", "代码块语言标记过长", 422)
                code_splitter = RecursiveCharacterTextSplitter(chunk_size=code_budget, chunk_overlap=0,
                    length_function=length, separators=["\n", " ", ""], keep_separator=True)
                fragments = [f"{opening}\n{fragment}\n{closing}" for fragment in code_splitter.split_text(code)]
            if len(fragments) > 1 and block["kind"] == "table_open":
                rows = body.splitlines()
                header = "\n".join(rows[:2])
                row_budget = budget - length(header + "\n")
                if row_budget < 8:
                    raise AppError("TABLE_HEADER_TOO_LONG", "表格表头过长，无法在分块限制内完整保留", 422)
                row_splitter = RecursiveCharacterTextSplitter(chunk_size=row_budget, chunk_overlap=0,
                    length_function=length, separators=["\n", "。", " ", ""], keep_separator=True)
                fragments = [header + "\n" + fragment for fragment in row_splitter.split_text("\n".join(rows[2:]))]
            # 递归切分单个无空格长串时不能默默截断，最终验证会严格检查硬上限。
            for fragment in fragments:
                parts.append({"path": path, "body": fragment, "lines": block["lines"],
                    "split_structure": atomic and len(fragments) > 1,
                    "asset_ids": re.findall(r"/assets/([a-f0-9]{32}-[a-f0-9]{64})", block["text"]),
                    "kind": block["kind"], "continuation": len(fragments) > 1})

    merged = []
    for part in parts:
        previous = merged[-1] if merged else None
        same_section = previous is not None and previous["path"] == part["path"]
        sibling = previous is not None and len(part["path"]) > 1 and previous["path"][:-1] == part["path"][:-1]
        # 同节普通段落合并到目标大小；兄弟短节才允许贪心合并，顶级主题不跨界。
        if previous and (same_section or sibling):
            short = length(previous["body"]) < settings.chunk_min_size or length(part["body"]) < settings.chunk_min_size
            prefix = " > ".join(part["path"])
            addition = part["body"] if same_section else f"{prefix}\n\n{part['body']}"
            candidate = previous["body"] + "\n\n" + addition
            candidate_length = length(" > ".join(previous["path"]) + "\n\n" + candidate)
            if candidate_length <= settings.chunk_size and (same_section or short) and not previous["continuation"] and not part["continuation"]:
                previous["body"] = candidate
                previous["lines"][1] = part["lines"][1]
                previous["paths"].append(part["path"])
                previous["asset_ids"].extend(part["asset_ids"])
                previous["split_structure"] |= part["split_structure"]
                continue
        merged.append({**part, "paths": [part["path"]]})

    chunks = []
    for index, item in enumerate(merged):
        text = (" > ".join(item["path"]) + "\n\n" + item["body"]).strip()
        count = length(text)
        if count > settings.chunk_max_size or len(text.encode("utf-8")) > 65535:
            raise AppError("CHUNK_TOO_LARGE", "分块超出 token 或向量字段字节限制，请调整分块配置", 422)
        digest = hashlib.sha256(f"{task['ingest_id']}:{index}:{text}".encode()).hexdigest()
        chunks.append({"chunk_id": digest, "document_id": task["document_id"], "ingest_id": task["ingest_id"],
            "chunk_index": index, "text": text, "metadata": {"filename": task["filename"],
                "section_paths": item["paths"], "line_range": item["lines"], "page_numbers": None,
                "asset_ids": sorted(set(item["asset_ids"])), "source_type": "mixed" if item["asset_ids"] else "text",
                "split_structure": item["split_structure"], "token_count": count,
                "embedding_model": settings.embedding_model, "embedding_revision": settings.embedding_revision,
                "pipeline_fingerprint": task["config_fingerprint"]}})
    if not chunks:
        raise AppError("EMPTY_DOCUMENT", "文档中没有可以向量化的有效正文", 422)
    return chunks
