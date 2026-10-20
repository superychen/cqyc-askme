"""LangGraph 文档导入节点；每个已确认阶段均可从落盘产物恢复。"""

import asyncio
import hashlib
import html
import logging
import re
from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from markdown_it import MarkdownIt

from askme.core.config import Settings
from askme.core.errors import AppError
from askme.integrations.clients import MinerU, ObjectStore, Vectors, describe, embeddings, image_mime
from askme.rag.splitting import split_markdown
from askme.services.storage import TaskStore, atomic_json, fingerprint, now, read_json

logger = logging.getLogger(__name__)


class ImportState(TypedDict):
    task_id: str


class ImportPipeline:
    def __init__(self, settings: Settings, store: TaskStore, task: dict, lease: str):
        self.settings, self.store, self.task, self.lease = settings, store, task, lease
        self.folder = store.folder(task["task_id"])
        self.output = store.output(task)
        self.output.mkdir(parents=True, exist_ok=True)

    async def owned(self):
        from askme.services.storage import LEASE
        if await self.store.redis.get(LEASE) != self.lease:
            raise AppError("LEASE_LOST", "任务执行权已失效", 503, True)

    async def progress(self, message: str, completed: int | None = None, total: int | None = None):
        await self.owned()
        step = next(row for row in self.task["steps"] if row["id"] == self.task["node"])
        step.update(completed=completed if completed is not None else step["completed"], total=total)
        await self.store.save(self.task, message, lease=self.lease)

    def node(self, name: str, operation):
        async def execute(state: ImportState):
            await self.owned()
            self.task["node"] = name
            step = next(row for row in self.task["steps"] if row["id"] == name)
            marker = self.folder / f"done-{name}.json"
            if marker.exists():
                self.task.update(read_json(marker).get("stats", {}))
                step["status"] = "succeeded"
                await self.progress("复用已完成阶段")
                return state
            step.update(status="running", started_at=now(), completed=0)
            await self.progress(f"正在{step['label']}")
            logger.info("Import node started task_id=%s node=%s attempt=%s", self.task["task_id"], name, self.task["attempt"])
            await operation()
            await self.owned()
            atomic_json(marker, {"finished_at": now(), "stats": {key: self.task[key] for key in
                ("chunk_count", "image_count", "section_count", "warnings")}})
            step.update(status="succeeded", finished_at=now())
            await self.progress(f"{step['label']}完成", step.get("total") or step["completed"], step.get("total"))
            logger.info("Import node completed task_id=%s node=%s", self.task["task_id"], name)
            return state
        return execute

    async def validate_source(self):
        if fingerprint(self.settings) != self.task["config_fingerprint"]:
            raise AppError("CONFIG_CHANGED", "任务配置已变更，请重新上传文档", 409)
        source = self.folder / f"source{self.task['suffix']}"
        digest = await asyncio.to_thread(lambda: hashlib.sha256(source.read_bytes()).hexdigest())
        if digest != self.task["sha256"]:
            raise AppError("SOURCE_CHANGED", "源文件校验失败，不能继续导入", 422)

    async def pdf_to_markdown(self):
        result = await MinerU(self.settings).parse(self.folder / "source.pdf", self.folder)
        atomic_json(self.folder / "parser-result.json", result)
        (self.output / "original.md").write_text(result["markdown"], encoding="utf-8")
        self.task["image_count"] = len(result["images"])

    async def load_markdown(self):
        text = (self.folder / "source.md").read_text(encoding="utf-8-sig")
        (self.output / "original.md").write_text(text, encoding="utf-8")
        (self.output / "enriched.md").write_text(text, encoding="utf-8")
        if re.search(r"!\[|<img\b", text, re.I):
            self.task["warnings"].append("MD 中的图片引用已保留，未自动读取外部或本地图片。")

    async def persist_assets(self):
        result = read_json(self.folder / "parser-result.json")
        items = result["images"]
        assets = read_json(self.folder / "assets.json") if (self.folder / "assets.json").exists() else []
        storage = ObjectStore(self.settings) if items else None
        for index, item in enumerate(items):
            await self.owned()
            asset_id = f"{self.task['task_id']}-{item['sha256']}"
            if not any(row["reference"] == item["reference"] for row in assets):
                raw = (self.folder / "parsed" / item["local_name"]).read_bytes()
                mime = await asyncio.to_thread(image_mime, raw)
                object_key = f"documents/{self.task['document_id']}/{self.task['ingest_id']}/images/{item['local_name']}"
                await asyncio.to_thread(storage.put, object_key, raw, mime)
                assets.append({**item, "asset_id": asset_id, "object_key": object_key, "mime": mime, "description": None})
                atomic_json(self.folder / "assets.json", assets)
            await self.progress("图片已保存至 MinIO", index + 1, len(items))
        atomic_json(self.folder / "assets.json", assets)
        if not assets:
            self.task["warnings"].append("PDF 未返回独立图片，图片增强阶段跳过。")

    async def describe_images(self):
        assets = read_json(self.folder / "assets.json")
        text = (self.output / "original.md").read_text(encoding="utf-8")
        storage = ObjectStore(self.settings) if assets else None
        for index, asset in enumerate(assets):
            await self.owned()
            if not asset["description"]:
                # 同一任务相同图片的多处引用共用描述，但保留每个引用的位置。
                cached = next((row["description"] for row in assets if row["sha256"] == asset["sha256"] and row["description"]), None)
                if cached:
                    asset["description"] = cached
                else:
                    raw = await asyncio.to_thread(storage.read, asset["object_key"])
                    position = text.find(asset["reference"])
                    context = text[max(0, position - 800):position + 800] if position >= 0 else ""
                    asset["description"] = await describe(self.settings, raw, asset["mime"], context)
                atomic_json(self.folder / "assets.json", assets)
            await self.progress("正在理解图片内容", index + 1, len(assets))

    async def enrich_markdown(self):
        text = (self.output / "original.md").read_text(encoding="utf-8")
        assets = read_json(self.folder / "assets.json")
        by_ref = {row["reference"]: row for row in assets}
        # MarkdownIt 的 image token 可处理带空格/括号的链接，替换完整图片语法而非拼接未转义 HTML。
        parser = MarkdownIt("commonmark", {"html": True})
        replacements = []
        for token in parser.parse(text):
            if token.type == "inline" and token.children:
                for child in token.children:
                    if child.type == "image" and child.attrGet("src") in by_ref:
                        item = by_ref[child.attrGet("src")]
                        replacements.append((child.attrGet("src"), item))

        def image_tag(item: dict, original: str = "") -> str:
            alt = f"{original} 图片识别补充：{item['description']}".strip()
            return f'<img src="/api/v1/admin/assets/{item["asset_id"]}" alt="{html.escape(alt, quote=True)}">'

        # 行内图片引用的目标地址由解析器识别；正则仅定位同一行中的完整语法。
        for ref, item in replacements:
            pattern = r"!\[([^\]]*)\]\(\s*<?" + re.escape(ref) + r'>?(?:\s+[\"\x27][^\n]*?[\"\x27])?\s*\)'
            text = re.sub(pattern, lambda match, item=item: image_tag(item, match[1]), text)
        def html_image(match):
            from html.parser import HTMLParser
            class Reader(HTMLParser):
                def handle_starttag(self, tag, attrs):
                    self.attrs = dict(attrs)
            reader = Reader()
            reader.feed(match.group())
            attrs = getattr(reader, "attrs", {})
            item = by_ref.get(attrs.get("src"))
            return image_tag(item, attrs.get("alt", "")) if item else match.group()
        text = re.sub(r"<img\b[^>]*>", html_image, text, flags=re.I)
        missing = [row for row in assets if row["reference"] in text and row["asset_id"] not in text]
        if missing:
            raise AppError("IMAGE_REFERENCE", "存在无法匹配的图片引用，需检查 MinerU 输出格式", 422)
        (self.output / "enriched.md").write_text(text, encoding="utf-8")

    async def normalize_document(self):
        text = (self.output / "enriched.md").read_text(encoding="utf-8")
        # 只统一换行与 BOM，不使用模型改写或激进删除页眉，保留用户事实。
        text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
        if not text.strip():
            raise AppError("EMPTY_DOCUMENT", "文档没有有效正文", 422)
        (self.folder / "normalized.md").write_text(text, encoding="utf-8")

    async def split_document(self):
        text = (self.folder / "normalized.md").read_text(encoding="utf-8")
        chunks = await asyncio.to_thread(split_markdown, text, self.settings, self.task)
        atomic_json(self.folder / "chunks.json", chunks)
        self.task["chunk_count"] = len(chunks)
        self.task["section_count"] = len({tuple(path) for row in chunks for path in row["metadata"]["section_paths"]})

    async def validate_chunks(self):
        chunks = read_json(self.folder / "chunks.json")
        if not chunks or len({row["chunk_id"] for row in chunks}) != len(chunks):
            raise AppError("INVALID_CHUNKS", "文档分块为空或主键重复", 422)
        self.task["chunk_count"] = len(chunks)

    async def embed_chunks(self):
        chunks = read_json(self.folder / "chunks.json")
        batch_size = self.settings.embedding_batch_size
        for start in range(0, len(chunks), batch_size):
            await self.owned()
            destination = self.folder / "vectors" / f"{start}.json"
            if not destination.exists():
                batch = chunks[start:start + batch_size]
                vectors = await embeddings(self.settings, [row["text"] for row in batch])
                atomic_json(destination, [{**row, "embedding": vector} for row, vector in zip(batch, vectors)])
            await self.progress("文本向量化", min(start + batch_size, len(chunks)), len(chunks))

    async def upsert_milvus(self):
        await self.owned()
        client = await asyncio.to_thread(Vectors, self.settings)
        try:
            await asyncio.to_thread(client.ensure)
            paths = sorted((self.folder / "vectors").glob("*.json"), key=lambda path: int(path.stem))
            completed = 0
            for path in paths:
                await self.owned()
                rows = read_json(path)
                # 重试重复 upsert 同一主键是有意的，处理“服务器写成功但客户端超时”的情况。
                await asyncio.to_thread(client.upsert, rows)
                completed += len(rows)
                await self.progress("分批写入 Milvus", completed, self.task["chunk_count"])
        finally:
            await asyncio.to_thread(client.close)

    async def verify_and_commit(self):
        chunks = read_json(self.folder / "chunks.json")
        client = await asyncio.to_thread(Vectors, self.settings)
        try:
            await asyncio.to_thread(client.ensure)
            await asyncio.to_thread(client.verify, chunks, self.task["ingest_id"])
            await self.owned()
            atomic_json(self.folder / "committed.json", {"ingest_id": self.task["ingest_id"], "chunk_count": len(chunks), "committed_at": now()})
            await self.store.redis.sadd("askme:committed", self.task["ingest_id"])
        finally:
            await asyncio.to_thread(client.close)

    async def run(self):
        # 即使校验节点已有完成标记，重试仍应确认源文件与配置没有发生变化。
        await self.validate_source()
        graph = StateGraph(ImportState)
        names = ["validate_source"]
        if self.task["suffix"] == ".pdf":
            names += ["pdf_to_markdown", "persist_assets", "describe_images", "enrich_markdown"]
            skipped = {"load_markdown"}
        else:
            names += ["load_markdown"]
            skipped = {"pdf_to_markdown", "persist_assets", "describe_images", "enrich_markdown"}
        names += ["normalize_document", "split_document", "validate_chunks", "embed_chunks", "upsert_milvus", "verify_and_commit"]
        for step in self.task["steps"]:
            if step["id"] in skipped:
                step["status"] = "skipped"
        for name in names:
            graph.add_node(name, self.node(name, getattr(self, name)))
        graph.add_edge(START, names[0])
        for left, right in zip(names, names[1:]):
            graph.add_edge(left, right)
        graph.add_edge(names[-1], END)
        await graph.compile().ainvoke({"task_id": self.task["task_id"]})
