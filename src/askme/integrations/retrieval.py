"""只读 Milvus 查询与 BGE rerank。查询图 → dense → rerank；不重建/删除向量。

分支共享的是并发容量，不共享结果。同步 RPC 在有界线程中执行；取消协程不能杀死
线程，因此槽位由真正完成的线程任务释放，避免反复取消导致后台线程无限增长。
"""
import asyncio
import hashlib
import json
import logging
import math
import re

import httpx
from pymilvus import MilvusClient

from askme.core.errors import AppError
from askme.integrations.clients import embeddings, auth

SLOTS = asyncio.Semaphore(4)
logger = logging.getLogger(__name__)


def excerpt(value: str, limit: int = 4000) -> str:
    """按 UTF-8 字节上界裁剪并脱敏；演示邮箱/手机号替换，不公开资料内部联系方式。

    字节数是保守 token 上界，并非使用 BGE tokenizer 估算生成模型的精确 token。
    """
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[联系方式已隐藏]", value)
    text = re.sub(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)", "[联系方式已隐藏]", text)
    return text.encode("utf-8")[:limit].decode("utf-8", errors="ignore")


def search_sync(settings, vector: list[float], committed: list[str]) -> tuple[list[dict], list[str]]:
    """线程内创建、查询并关闭客户端；只对已提交 ingest_id 检索，空集合不伪装成功。

    上游 dense 传入服务配置、1024 维查询向量、Redis 中的已提交版本列表。
    顺序为校验集合 → 必要时从磁盘恢复提交索引 → 加载/检索 → 规范化证据 → 关闭连接。
    返回 (候选列表, 校验后的版本 ID)，候选供图中的 RRF 与 rerank 节点继续处理。
    演示未提交版本 B 即使相似度更高也不参与 ANN；committed=[] 且无有效磁盘记录时
    使用永不匹配条件，正常返回空候选。filter 只使用验证过的十六进制 ID，不拼接问题。
    演示命中 {chunk_id:'demo-chunk', entity:{text:'演示正文', ingest_id:有效版本}}
    转为 {id:'rag:demo-chunk', text:'演示正文', ...}；不能假定 SDK 总返回名为 id 的键。
    集合/维度异常、版本越界或命中缺少主键时明确失败，不伪装成资料为空；finally
    在成功、空结果和异常时都关闭本线程客户端，线程容量由 dense 的完成回调释放。
    """
    client = MilvusClient(uri=settings.milvus_uri, token=settings.milvus_token.get_secret_value(), timeout=10)
    try:
        if not client.has_collection(settings.milvus_collection):
            raise AppError("KNOWLEDGE_NOT_READY", "知识库尚未就绪", 503)
        fields = {f["name"]: f for f in client.describe_collection(settings.milvus_collection)["fields"]}
        if int(fields.get("embedding", {}).get("params", {}).get("dim", 0)) != 1024:
            raise AppError("KNOWLEDGE_SCHEMA", "知识库向量配置不匹配", 503)
        ids = [v for v in committed if re.fullmatch(r"[a-f0-9]{32}", v)]
        if not ids:
            # Redis重启丢索引时，只恢复磁盘明确提交且Milvus条数吻合的版本；不能把所有向量公开。
            # 演示 marker写了10块但库内只有9块，忽略该版本，等待管理员重试导入。
            for marker in settings.resolve(settings.import_work_dir).glob("*/committed.json"):
                try:
                    payload = json.loads(marker.read_text(encoding="utf-8"))
                    identity = payload["ingest_id"]
                    if not re.fullmatch(r"[a-f0-9]{32}", identity) or not isinstance(payload["chunk_count"], int):
                        continue
                    counts = client.query(settings.milvus_collection, filter=f'ingest_id == "{identity}"', output_fields=["count(*)"], consistency_level="Strong", timeout=5)
                    if counts and counts[0]["count(*)"] == payload["chunk_count"] and payload["chunk_count"] > 0:
                        ids.append(identity)
                except (OSError, ValueError, KeyError, TypeError):
                    continue
        expression = 'ingest_id in [' + ','.join('"'+v+'"' for v in ids) + ']' if ids else 'ingest_id == ""'
        client.load_collection(settings.milvus_collection)
        groups = client.search(collection_name=settings.milvus_collection, data=[vector], anns_field="embedding",
                               search_params={"metric_type": "COSINE", "params": {"ef": max(64, settings.rag_top_k)}},
                               filter=expression, limit=settings.rag_top_k, output_fields=["chunk_id", "text", "metadata", "ingest_id"],
                               consistency_level="Strong", timeout=10)
        hits = []
        for rank, row in enumerate(groups[0] if groups else [], 1):
            entity = row["entity"]
            if entity["ingest_id"] not in ids:
                raise AppError("KNOWLEDGE_VERSION", "知识库版本校验失败", 503)
            # 新版 PyMilvus Hit 以实际主键 chunk_id 返回，旧版可用通用 id。
            # 显式请求 chunk_id 后优先读取业务主键，同时兼容它位于 entity 的响应。
            # 演示旧版 {id:'demo-chunk', entity:{...}} 仍得到相同稳定证据 ID；
            # 主键全缺失时不能用排名临时替代，否则 RRF 去重与引用归属可能错乱。
            chunk_id = row.get("chunk_id") or entity.get("chunk_id") or row.get("id")
            if not isinstance(chunk_id, str) or not chunk_id.strip():
                logger.warning("RAG result rejected reason=missing_chunk_id rank=%s", rank)
                raise AppError("KNOWLEDGE_RESULT", "知识库检索结果格式不匹配", 503)
            meta = entity.get("metadata") or {}
            # 仅公开文件名，不暴露 Windows/Linux 路径；每条证据的正文先过滤再送 LLM。
            title = str(meta.get("filename", "个人资料")).replace("\\", "/").rsplit("/", 1)[-1]
            hits.append({"id": "rag:"+chunk_id, "source_type": "rag", "title": excerpt(title, 200),
                         "text": excerpt(entity["text"]), "rank": rank, "url": None})
        # 只输出统计值，避免日志包含个人文档正文、文件名或向量内容。
        logger.debug("RAG search completed candidates=%s committed_versions=%s", len(hits), len(ids))
        return hits, ids
    finally:
        client.close()


async def dense(settings, redis, query: str) -> list[dict]:
    """BGE-M3 编码后真实查询 Milvus；20 秒外层预算由 rag_retrieve 管理。

    等槽最多 5 秒。shield 防止取消将运行线程任务标记完成并过早归还容量。
    """
    committed = sorted(await redis.smembers("askme:committed"))
    vector = (await embeddings(settings, [query]))[0]
    async with asyncio.timeout(5):
        await SLOTS.acquire()
    task = asyncio.create_task(asyncio.to_thread(search_sync, settings, vector, committed))
    def release(done):
        """实际 RPC 完成后释放槽位并消费迟到异常；不将结果写回已经取消的图。"""
        SLOTS.release()
        if not done.cancelled():
            done.exception()
    task.add_done_callback(release)
    hits, valid_ids = await asyncio.shield(task)
    if not committed and valid_ids:
        await redis.sadd("askme:committed", *valid_ids)
    return hits


def fuse(rag: list[dict], web: list[dict]) -> list[dict]:
    """两路排名按 RRF(k=60) 合并，稳定去重；演示单路第1名=1/61。

    来源不同但正文相同也合并，优先保留 RAG 归属；原始余弦/搜索分不直接相加。
    """
    merged = {}
    for rows in (rag, web):
        seen = set()
        for rank, row in enumerate(rows, 1):
            key = hashlib.sha256(row["text"].strip().encode()).hexdigest()
            if key in seen:
                continue
            seen.add(key)
            if key not in merged:
                merged[key] = {**row, "rrf_score": 0.0}
            merged[key]["rrf_score"] += 1/(60+rank)
    return sorted(merged.values(), key=lambda row: (-row["rrf_score"], row["id"]))


async def rerank(settings, query: str, rows: list[dict]) -> list[dict]:
    """Infinity /rerank 输出归一化分数；空输入跳过，异常绝不回退成余弦分。

    演示 results=[{index:1,relevance_score:0.8}] 必须覆盖全部输入后才接受。
    raw_scores=false 要求服务返回 sigmoid 分，本地不重复 sigmoid，越界即协议错误。
    """
    if not rows:
        return []
    if not settings.rerank_base_url:
        raise AppError("RERANK_NOT_CONFIGURED", "重排序服务尚未配置", 503)
    async with httpx.AsyncClient(timeout=15, follow_redirects=False, headers=auth(settings.rerank_api_key.get_secret_value())) as client:
        response = await client.post(settings.rerank_base_url.rstrip("/")+"/rerank", json={
            "model": settings.rerank_model, "query": excerpt(query, 3000), "documents": [r["text"] for r in rows],
            "raw_scores": False, "return_documents": False})
        response.raise_for_status()
        payload = response.json()
    try:
        results = payload["results"]
        if sorted(r["index"] for r in results) != list(range(len(rows))):
            raise ValueError()
        ranked = []
        for result in results:
            score = float(result["relevance_score"])
            if not math.isfinite(score) or not 0 <= score <= 1:
                raise ValueError()
            ranked.append({**rows[result["index"]], "score": score})
        return sorted(ranked, key=lambda r: (-r["score"], r["id"]))
    except (KeyError, TypeError, ValueError):
        raise AppError("RERANK_PROTOCOL", "重排序服务结果格式不匹配", 502) from None
