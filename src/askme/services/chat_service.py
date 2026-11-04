"""连接图和 HTTP 的生命周期：预占由路由完成，run 负责租约、流、取消与最终结算。

每个请求一个有界队列；emit 顺序编号，分支只发布业务进度，不竞争共享 seq。
刷新不重跑 LLM；浏览器读 request_status 恢复已存正文。示例均为演示数据。
"""
import asyncio
import contextlib
import json
import logging
import time

from askme.core.errors import AppError
from askme.services.chat_store import ChatStore, PREFIX

logger = logging.getLogger(__name__)


class ChatService:
    """应用级图容器，state/context 均按请求新建；shutdown 取消所有活动请求。"""
    def __init__(self, settings, redis):
        """延迟导入图，基础 health/admin 不要求启用 RAG 可选依赖。"""
        self.settings, self.redis = settings, redis
        self.graph = None
        self.tasks = set()

    async def events(self, store, operation):
        """异步生成 SSE 帧；15 秒心跳；断线取消图并等待有限清理，不重新生成。

        演示 answer_delta 在 Redis 确认后入队；done 之后队列结束。队列容量64防慢客户端
        无限堆积，断线设置 disconnected 让收尾不再往无人消费的队列写入。
        """
        from askme.rag.chat_graph import ChatContext, build_graph
        if self.graph is None:
            self.graph = build_graph()
        queue = asyncio.Queue(maxsize=64)
        disconnected = asyncio.Event()
        seq = 0

        async def emit(event, data):
            """同事件循环内分配唯一递增 seq；不 await 前完成编号，避免两路重复编号。"""
            nonlocal seq
            if disconnected.is_set():
                return
            seq += 1
            payload = {**data, "request_id": operation["request_id"], "turn_id": operation["turn_id"], "seq": seq}
            await queue.put("event: "+event+"\ndata: "+json.dumps(payload, ensure_ascii=False, allow_nan=False)+"\n\n")

        ctx = ChatContext(self.settings, store, operation, emit)

        async def execute():
            """图失败/取消都结算；只把安全错误码和中文信息给前端，不透传外部异常。"""
            try:
                await emit("meta", {"remaining": (await store.snapshot())["remaining"]})
                async with asyncio.timeout(self.settings.chat_timeout):
                    async for _ in self.graph.astream({"question": operation["question"]}, context=ctx, stream_mode="updates", config={"max_concurrency": 2}):
                        pass
            except (Exception, asyncio.CancelledError) as exc:
                if not ctx.finished:
                    message = (exc.message if isinstance(exc, AppError) else "回答已停止" if isinstance(exc, asyncio.CancelledError) else "问答服务暂时不可用，请稍后重试")
                    code = exc.code if isinstance(exc, AppError) else "CHAT_CANCELLED" if isinstance(exc, asyncio.CancelledError) else "CHAT_FAILED"
                    logger.warning("Chat interrupted request_id=%s code=%s error_type=%s", operation["request_id"], code, type(exc).__name__)
                    try:
                        # 被取消的任务仍须有限清理；如果 Redis 故障，失效租约由 sweeper 回收。
                        ctx.final_result = await asyncio.wait_for(asyncio.shield(store.finish(operation["owner"], "failed", error=message)), timeout=5)
                        ctx.finished = True
                    except Exception as cleanup:
                        logger.error("Chat cleanup deferred request_id=%s error_type=%s", operation["request_id"], type(cleanup).__name__)
                    await emit("error", {"code": code, "message": message, "retryable": True})
            finally:
                if ctx.finished:
                    await emit("done", ctx.final_result or {})
                if not disconnected.is_set():
                    await queue.put(None)

        execution = asyncio.create_task(execute())
        self.tasks.add(execution)

        async def renew():
            """每3秒读取跨进程取消标志并续租；失去所有权时取消执行，拒绝迟到写入。"""
            while not execution.done():
                await asyncio.sleep(3)
                if ctx.finished:
                    return
                try:
                    if not await store.heartbeat(operation["owner"]):
                        execution.cancel()
                        return
                except Exception:
                    if not ctx.finished:
                        execution.cancel()
                    return

        renewal = asyncio.create_task(renew())
        try:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=15)
                except TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    break
                yield item
        finally:
            disconnected.set()
            renewal.cancel()
            if not execution.done():
                execution.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(execution, timeout=8)
            with contextlib.suppress(asyncio.CancelledError):
                await renewal
            self.tasks.discard(execution)

    async def sweep(self):
        """后台回收崩溃后的租约，每15秒最多100个；没有访问也不会永久遗留预占。

        错误只记录类型并等下一轮；正常取消不捕获，供 lifespan 关闭。
        """
        while True:
            try:
                expired = await self.redis.zrangebyscore(PREFIX+"active", "-inf", time.time(), start=0, num=100)
                for identity in expired:
                    await ChatStore(self.redis, identity, self.settings.chat_history_ttl).snapshot()
            except Exception as exc:
                logger.warning("Chat recovery deferred error_type=%s", type(exc).__name__)
            await asyncio.sleep(15)

    async def close(self):
        """关闭时取消现有请求并等待结算，再让 lifespan 关闭 Redis；避免先断开存储。"""
        pending = list(self.tasks)
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.wait(pending, timeout=8)
