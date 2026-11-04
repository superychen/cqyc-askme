"""统一 LangChain 模型入口：查询图和历史图片摘要都从这里创建模型。

阅读顺序：model 上下文 → structured/节点内 astream → finally 释放两个 SDK 客户端。
所有示例为演示；未配凭据就失败，不能返回假答案。关闭 SDK 自动重试以避免重复计费。
"""
from contextlib import asynccontextmanager
import asyncio
import json
import logging

from langchain_deepseek import ChatDeepSeek
from langchain_core.messages import SystemMessage, HumanMessage

from askme.core.errors import AppError

logger = logging.getLogger(__name__)


@asynccontextmanager
async def model(settings, *, tokens: int = 1200, timeout: int = 60, retries: int = 0):
    """返回 LangChain ChatDeepSeek，退出时关闭连接；演示 tokens=256 用于查询分类。

    extra_body 由 SDK 放在请求顶层，与原图片摘要 thinking=disabled 行为一致。
    新问答 retries=0；历史图片摘要可显式传3保留有界重试，不影响公开问答计费规则。
    不共享请求 messages，也不把供应商原始异常（可能含正文）写进日志。
    """
    if not settings.deepseek_api_key.get_secret_value():
        raise AppError("LLM_NOT_CONFIGURED", "回答服务尚未配置", 503)
    client = ChatDeepSeek(model=settings.deepseek_model, api_key=settings.deepseek_api_key,
                         base_url=settings.deepseek_base_url, max_tokens=tokens, timeout=timeout,
                         max_retries=retries, temperature=0, extra_body={"thinking": {"type": "disabled"}})
    try:
        yield client
    finally:
        # 原生 SDK 同时构造同步和异步客户端，即使只调用异步也要释放两者。
        await client.root_async_client.close()
        client.root_client.close()


async def structured(settings, schema, instruction: str, payload: dict, *, tokens: int = 800):
    """LangChain JSON 模式＋Pydantic 本地校验；缺字段/非法枚举失败，不猜字段含义。

    演示 schema=QueryPlan，输出 intent='personal'；乱码 JSON 抛出异常，由所属节点
    决定回退原问题或走保守门控。10 秒包括外呼和解析，不输出模型思考内容。
    """
    async with asyncio.timeout(10), model(settings, tokens=tokens, timeout=10) as llm:
        prompt = instruction + "\n只输出符合以下 JSON Schema 的 JSON 对象：\n" + json.dumps(schema.model_json_schema(), ensure_ascii=False)
        response = await llm.ainvoke([SystemMessage(content=prompt), HumanMessage(content=json.dumps(payload, ensure_ascii=False))],
                                    response_format={"type": "json_object"})
        if response.response_metadata.get("finish_reason") == "length":
            raise AppError("LLM_TRUNCATED", "模型输出不完整", 502, True)
        return schema.model_validate_json(response.content)
