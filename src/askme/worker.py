"""独立导入 Worker：单消费者租约、阶段恢复及 Redis Streams 确认。"""

import asyncio
import contextlib
import logging
import signal
import uuid

from redis.asyncio import Redis
from redis.exceptions import RedisError, ResponseError

from askme.core.config import get_settings
from askme.core.errors import AppError, error_location
from askme.rag.import_graph import ImportPipeline
from askme.services.storage import GROUP, LEASE, QUEUE, TaskStore, fingerprint, now, read_json

logger = logging.getLogger(__name__)


async def recover(store: TaskStore, lease: str):
    """磁盘为恢复依据；在拿到全局租约后对账，允许 Redis 全量重建状态索引。"""
    for path in store.root.glob("*/manifest.json"):
        try:
            task = read_json(path)
            if task.get("dedup_key"):
                await store.redis.set(task["dedup_key"], task["task_id"])
            committed = path.parent / "committed.json"
            if committed.exists():
                await store.redis.sadd("askme:committed", task["ingest_id"])
                task.update(status="succeeded", error=None)
                for step in task["steps"]:
                    if step["status"] == "running":
                        step["status"] = "succeeded"
                await store.save(task, "已恢复完成记录", lease=lease)
            elif task["status"] in {"queued", "running"}:
                task["status"] = "queued"
                await store.save(task, "Worker 已恢复，等待继续处理", enqueue=True, lease=lease)
            else:
                await store.save(task, task.get("message", ""), lease=lease)
        except (ValueError, KeyError, OSError):
            logger.error("Invalid task manifest skipped task_id=%s", path.parent.name)


async def execute(store: TaskStore, task: dict, lease: str):
    task.update(status="running", error=None)
    await store.save(task, "开始处理文档", lease=lease)
    try:
        if fingerprint(store.settings) != task["config_fingerprint"]:
            raise AppError("CONFIG_CHANGED", "处理配置已变更，请重新上传文档", 409)
        await ImportPipeline(store.settings, store, task, lease).run()
        task.update(status="succeeded", finished_at=now(), error=None)
        await store.save(task, "导入完成，所有向量已校验", lease=lease)
        logger.info("Import succeeded task_id=%s chunks=%s", task["task_id"], task["chunk_count"])
    except asyncio.CancelledError:
        task["status"] = "interrupted"
        task["error"] = {"code": "WORKER_STOPPED", "message": "Worker 已停止，可手动重试", "retryable": True, "failed_node": task.get("node")}
        with contextlib.suppress(RedisError, AppError):
            await store.save(task, "任务中断", lease=lease)
        raise
    except Exception as exc:
        if isinstance(exc, AppError) and exc.code == "LEASE_LOST":
            raise
        if isinstance(exc, RedisError):
            raise
        safe = exc if isinstance(exc, AppError) else AppError("IMPORT_FAILED", "处理失败，请根据任务编号检查服务配置和日志", 502, True)
        if isinstance(exc, TimeoutError):
            safe = AppError("NODE_TIMEOUT", "处理超时，请检查外部服务后重试", 503, True)
        task["status"] = "failed"
        task["error"] = {"code": safe.code, "message": safe.message, "retryable": safe.retryable, "failed_node": task.get("node")}
        for step in task["steps"]:
            if step["id"] == task.get("node"):
                step.update(status="failed", finished_at=now())
        await store.save(task, safe.message, lease=lease)
        logger.error("Import failed task_id=%s node=%s code=%s error_type=%s locations=%s", task["task_id"], task.get("node"), safe.code, type(exc).__name__, error_location(exc))


async def keep_lease(redis: Redis, token: str):
    while True:
        await asyncio.sleep(10)
        renewed = await redis.eval("if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('EXPIRE',KEYS[1],60) end return 0", 1, LEASE, token)
        if not renewed:
            raise AppError("LEASE_LOST", "Worker 执行权已失效", 503, True)


async def consume(store: TaskStore, token: str, stopping: asyncio.Event):
    # 固定消费者名配合全局租约，重启后先读取该消费者未 ACK 消息，再接收新消息。
    pending = True
    while not stopping.is_set():
        batches = await store.redis.xreadgroup(GROUP, "single-worker", {QUEUE: "0" if pending else ">"}, count=1, block=2000)
        if not batches or not batches[0][1]:
            pending = False
            continue
        for _, messages in batches:
            for message_id, fields in messages:
                task = await store.get(fields["task_id"])
                if task["status"] in {"queued", "running"}:
                    await execute(store, task, token)
                # 同一任务恢复时可能再次入队，终态消息仅 ACK，不再执行副作用。
                await store.redis.xack(QUEUE, GROUP, message_id)


async def main():
    settings = get_settings()
    logging.basicConfig(level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # HTTP 客户端默认 info 日志会打印上游 URL；只保留经过脱敏的应用日志。
    for name in ("httpx", "httpcore", "pymilvus"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True, socket_connect_timeout=3, socket_timeout=10)
    token, stopping = uuid.uuid4().hex, asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stopping.set)
    jobs = []
    try:
        await redis.ping()
        if not await redis.set(LEASE, token, nx=True, ex=60):
            raise AppError("WORKER_EXISTS", "已有导入 Worker 正在执行，请勿重复启动", 409)
        try:
            await redis.xgroup_create(QUEUE, GROUP, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise
        heartbeat = asyncio.create_task(keep_lease(redis, token))
        jobs.append(heartbeat)
        await recover(TaskStore(settings, redis), token)
        consumer = asyncio.create_task(consume(TaskStore(settings, redis), token, stopping))
        stopper = asyncio.create_task(stopping.wait())
        jobs.extend([consumer, stopper])
        logger.info("Import worker started")
        completed, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
        for job in completed:
            job.result()
    except (RedisError, AppError) as exc:
        logger.error("Worker stopped code=%s error_type=%s", getattr(exc, "code", "REDIS_UNAVAILABLE"), type(exc).__name__)
        raise SystemExit(1) from None
    finally:
        for job in jobs:
            job.cancel()
        await asyncio.gather(*jobs, return_exceptions=True)
        with contextlib.suppress(RedisError):
            await redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, LEASE, token)
        await redis.aclose()
        logger.info("Import worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
