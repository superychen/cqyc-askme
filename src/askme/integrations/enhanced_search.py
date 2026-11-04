"""百炼 EnhancedSearch MCP → LangChain Tool → 规范网页证据。

参考其他项目的协议边界，但不假设实际服务已经联调。参数和返回字段不匹配时明确
失败，图的 Web 分支记录 failed；不能把接口错误伪装成网上没有结果。
"""
import hashlib
import json
from urllib.parse import urlsplit, urlunsplit

import httpx2
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from langchain.mcp import as_langchain_tool
from jsonschema import validate

from askme.core.errors import AppError
from askme.integrations.retrieval import excerpt


def http_factory(**kwargs):
    """MCP 传输工厂强制禁止重定向，防止 Bearer 头被转发到其他主机。"""
    kwargs["follow_redirects"] = False
    kwargs["timeout"] = 15
    return httpx2.AsyncClient(**kwargs)


def parse_pages(payload: dict) -> list[dict]:
    """解析明确支持的 pages 协议；演示 pages=[] 是正常空结果，缺 pages 是错误。

    只保留 HTTP(S) 链接和经过长度/敏感字段过滤的摘要，不抓取任意网页全文。
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("pages"), list):
        raise AppError("WEB_PROTOCOL", "搜索服务返回格式尚未适配", 502)
    rows, seen = [], set()
    for page in payload["pages"]:
        if not isinstance(page, dict) or not all(isinstance(page.get(k), str) for k in ("title", "url", "snippet")):
            raise AppError("WEB_PROTOCOL", "搜索服务网页字段不匹配", 502)
        url = urlsplit(page["url"])
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
            continue
        link = urlunsplit((url.scheme, url.netloc, url.path, url.query, ""))
        if link in seen or not page["snippet"].strip():
            continue
        seen.add(link)
        rows.append({"id": "web:"+hashlib.sha256(link.encode()).hexdigest(), "source_type": "web",
                     "url": link, "title": excerpt(page["title"], 200), "text": excerpt(page["snippet"]), "rank": len(rows)+1})
    if payload["pages"] and not rows:
        raise AppError("WEB_PROTOCOL", "搜索服务没有有效网页条目", 502)
    return rows[:5]


async def search(settings, redis, query: str) -> list[dict]:
    """发现指定工具、按真实 schema 校验 query/count，再通过 LangChain ainvoke 调用。

    全局预算原子预扣且不自动退款/重试（超时可能已计费）。演示 budget=0 不外呼；
    tool schema 新增未知必填参数时 fail closed，等待管理员适配，不凭空填入参数。
    """
    url = urlsplit(settings.bailian_search_mcp_url)
    if (url.scheme != "https" or url.hostname != "dashscope.aliyuncs.com" or url.username or url.password
            or url.query or url.fragment or url.path.rstrip("/") != "/api/v1/mcps/EnhancedSearch/mcp"
            or not settings.bailian_search_tool_name or not settings.dashscope_api_key.get_secret_value()):
        raise AppError("WEB_NOT_CONFIGURED", "联网搜索尚未配置", 503)
    # 不传身份、历史、简历；查询还需经过图节点的主题抽象和敏感字段检查。
    budget = await redis.eval("local n=tonumber(redis.call('GET',KEYS[1]) or '0'); if n>=tonumber(ARGV[1]) then return 0 end; redis.call('INCR',KEYS[1]); return 1", 1,
                              "askme:public:web_used", settings.web_search_budget)
    if not budget:
        raise AppError("WEB_BUDGET", "本期联网搜索预算已用完", 503)
    transport = StreamableHttpTransport(settings.bailian_search_mcp_url,
                headers={"Authorization": "Bearer "+settings.dashscope_api_key.get_secret_value()}, httpx_client_factory=http_factory)
    async with Client(transport) as client:
        catalog = await client.list_tools()
        remote = next((t for t in catalog if t.name == settings.bailian_search_tool_name), None)
        if remote is None:
            raise AppError("WEB_TOOL_MISSING", "未找到配置的搜索工具", 503)
        args = {"query": query}
        if "count" in remote.input_schema.get("properties", {}):
            args["count"] = 5
        validate(args, remote.input_schema)
        tool = await as_langchain_tool(remote, client)
        message = await tool.ainvoke({"type": "tool_call", "id": "web-search", "name": tool.name, "args": args})
        if message.status == "error":
            raise AppError("WEB_TOOL_ERROR", "搜索工具暂时不可用", 502)
        artifact = message.artifact or {}
        payload = artifact.get("structured_content")
        if payload is None:
            blocks = message.content if isinstance(message.content, list) else [{"type": "text", "text": message.content}]
            texts = [b["text"] for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
            if len(texts) != 1:
                raise AppError("WEB_PROTOCOL", "搜索结果结构不匹配", 502)
            payload = json.loads(texts[0])
        return parse_pages(payload)
