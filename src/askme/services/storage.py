"""磁盘任务清单和 Redis 状态；大正文和图片不会进入进度事件。"""

import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from redis.asyncio import Redis

from askme.core.config import Settings
from askme.core.errors import AppError

QUEUE = "askme:imports:queue"
GROUP = "askme-importers"
LEASE = "askme:imports:worker"
NODES = ["validate_source", "pdf_to_markdown", "load_markdown", "persist_assets", "describe_images", "enrich_markdown", "normalize_document", "split_document", "validate_chunks", "embed_chunks", "upsert_milvus", "verify_and_commit"]
LABELS = ["校验文档", "PDF 解析", "读取 Markdown", "图片存储", "图片理解", "补充图片描述", "整理文档结构", "语义切分", "检查分块", "文本向量化", "写入向量库", "校验并提交"]


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, data) -> None:
    """先写同目录临时文件并 fsync，再原子替换，防止崩溃留下半份清单。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fingerprint(settings: Settings) -> str:
    # 处理规则或模型变更必须产生新导入版本，不能复用旧向量。
    fields = (settings.embedding_model, settings.embedding_revision, settings.tokenizer_path,
              settings.chunk_size, settings.chunk_min_size, settings.chunk_max_size,
              settings.chunk_overlap, settings.deepseek_model, settings.mineru_tier,
              settings.embedding_batch_size, "pipeline-v1")
    return hashlib.sha256(json.dumps(fields).encode()).hexdigest()


def check_id(value: str) -> str:
    if len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise AppError("NOT_FOUND", "记录不存在", 404)
    return value


class TaskStore:
    def __init__(self, settings: Settings, redis: Redis):
        self.settings, self.redis = settings, redis
        self.root = settings.resolve(settings.import_work_dir)

    def folder(self, task_id: str) -> Path:
        return self.root / check_id(task_id)

    def output(self, task: dict) -> Path:
        return self.settings.resolve(self.settings.pdf_to_md_dir) / check_id(task["document_id"]) / check_id(task["ingest_id"])

    async def get(self, task_id: str) -> dict:
        check_id(task_id)
        raw = await self.redis.get(f"askme:task:{task_id}")
        if raw:
            return json.loads(raw)
        path = self.folder(task_id) / "manifest.json"
        if path.exists():
            return read_json(path)
        raise AppError("NOT_FOUND", "导入任务不存在", 404)

    async def save(self, task: dict, message: str = "", enqueue: bool = False, lease: str = "") -> None:
        task["updated_at"] = now()
        task["message"] = message or task.get("message", "")
        payload = json.dumps({key: value for key, value in task.items() if key != "event_id"}, ensure_ascii=False)
        # 事件、游标和快照原子更新。Worker 必须仍持有租约，旧执行者不能发布完成状态。
        script = """
        if ARGV[3] ~= '' and redis.call('GET', KEYS[4]) ~= ARGV[3] then return false end
        local id = redis.call('XADD', KEYS[2], 'MAXLEN', '~', 1000, '*', 'data', ARGV[1])
        local task = cjson.decode(ARGV[1])
        local snapshot = string.sub(ARGV[1], 1, -2) .. ',"event_id":"' .. id .. '"}'
        redis.call('SET', KEYS[1], snapshot)
        redis.call('ZADD', KEYS[3], ARGV[2], task['task_id'])
        if ARGV[4] == '1' then redis.call('XADD', KEYS[5], '*', 'task_id', task['task_id']) end
        return id
        """
        # 不用 Lua cjson 重新编码快照：它会把 Python 的空列表转换为空对象。
        event_id = await self.redis.eval(script, 5, f"askme:task:{task['task_id']}",
            f"askme:events:{task['task_id']}", "askme:tasks", LEASE, QUEUE,
            payload, datetime.fromisoformat(task["created_at"]).timestamp(), lease, "1" if enqueue else "0")
        if not event_id:
            raise AppError("LEASE_LOST", "任务执行权已失效", 503, True)
        task["event_id"] = event_id
        atomic_json(self.folder(task["task_id"]) / "manifest.json", task)

    async def recent(self, offset: int, limit: int) -> dict:
        ids = await self.redis.zrevrange("askme:tasks", offset, offset + limit - 1)
        return {"items": [await self.get(item) for item in ids], "total": await self.redis.zcard("askme:tasks")}

    def new(self, task_id: str, suffix: str, filename: str, digest: str, size: int) -> dict:
        return {"task_id": task_id, "document_id": uuid.uuid4().hex, "ingest_id": uuid.uuid4().hex,
            "filename": filename, "suffix": suffix, "sha256": digest, "size": size,
            "config_fingerprint": fingerprint(self.settings), "status": "queued", "attempt": 1,
            "created_at": now(), "updated_at": now(), "node": None, "message": "等待处理",
            "steps": [{"id": key, "label": label, "status": "pending", "completed": 0, "total": None}
                for key, label in zip(NODES, LABELS)], "warnings": [], "error": None,
            "chunk_count": 0, "image_count": 0, "section_count": 0}
