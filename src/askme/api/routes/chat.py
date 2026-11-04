"""公开 HTTP 入口：身份/来源/输入→ChatStore→ChatService→SSE。

无管理员登录依赖；任何 request_id/选项查询都限定本 IP。浏览器无需持有身份令牌。
演示 GET bootstrap 不扣次，POST messages 才预占；重复 UUID 返回恢复快照。
"""
import json
from uuid import UUID

from fastapi import APIRouter, Request, Query
from fastapi.responses import StreamingResponse

from askme.core.config import get_settings
from askme.core.errors import AppError
from askme.schemas.chat import Question, Selection, SUGGESTIONS
from askme.services.chat_store import ChatStore
from askme.services.visitor import visitor_id, public_origin

router = APIRouter(prefix="/chat", tags=["公开问答"])


def store_for(request: Request) -> ChatStore:
    """只从服务端来源构建存储，前端无法提交另一人的 ip_id。缺密钥返回503。"""
    settings = get_settings()
    return ChatStore(request.app.state.redis, visitor_id(request, settings), settings.chat_history_ttl)


async def rate_limit(store):
    """每 IP 每分钟最多10次提交，失败也占速率但不占永久额度；Lua避免首次过期竞态。"""
    count = await store.redis.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],60) end; return n", 1, "askme:public:rate:"+store.ip_id)
    if count > 10:
        raise AppError("RATE_LIMITED", "提问过于频繁，请稍后再试", 429, True)


def public_turn(turn):
    """筛选公开字段；requests/digest 是内部幂等数据，不能暴露到页面。"""
    return {k: v for k, v in turn.items() if k in {"id", "question", "selection", "answer", "citations", "status", "created_at", "clarification", "error"}}


@router.get("/bootstrap")
async def bootstrap(request: Request):
    """返回额度和推荐池；演示 remaining=0 时仍能读历史和已付费的澄清。"""
    snapshot = await store_for(request).snapshot()
    return {"remaining": snapshot["remaining"], "active_request": snapshot["active_request"], "suggestions": SUGGESTIONS, "limit": 20}


@router.get("/history")
async def history(request: Request, before: str | None = None, limit: int = Query(20, ge=1, le=60)):
    """按 turn ID 向前分页，不使用问题正文作游标；最初无 before 取最近20轮。"""
    snapshot = await store_for(request).snapshot()
    turns = snapshot["turns"]
    if before:
        index = next((i for i, t in enumerate(turns) if t["id"] == before), None)
        if index is None:
            raise AppError("INVALID_CURSOR", "历史记录已变化，请刷新", 422)
        turns = turns[:index]
    selected = turns[-limit:]
    return {"items": [public_turn(t) for t in selected], "has_more": len(turns)>limit, "remaining": snapshot["remaining"], "active_request": snapshot["active_request"]}


async def response_for(request, store, operation):
    """重复提交仅回放快照事件，不再运行图；首次生成才调用 events。

    响应头关闭代理缓存/缓冲；进入 SSE 后由 error 事件报告故障。
    """
    if operation["duplicate"]:
        snapshot = await store.request_status(operation["request_id"])
        if snapshot.get("turn"):
            snapshot["turn"] = public_turn(snapshot["turn"])
        async def replay():
            """已持久化结果作为 snapshot 事件交给页面；running 状态继续查询而不重生成。"""
            yield "event: snapshot\ndata: "+json.dumps(snapshot, ensure_ascii=False)+"\n\n"
        stream = replay()
    else:
        stream = request.app.state.chat.events(store, operation)
    return StreamingResponse(stream, media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.post("/messages")
async def messages(request: Request, data: Question):
    """校验来源→速率→原子预占→SSE；前置错误使用标准HTTP，不产生空200流。"""
    public_origin(request, get_settings())
    store = store_for(request)
    await rate_limit(store)
    operation = await store.begin(str(data.request_id), data.message)
    return await response_for(request, store, operation)


@router.post("/clarifications/{clarification_id}/select")
async def select(request: Request, clarification_id: str, data: Selection):
    """消费本 IP 有效卡片，继承原问题计次；非法/过期/重复选择不启动新模型调用。"""
    public_origin(request, get_settings())
    store = store_for(request)
    await rate_limit(store)
    operation = await store.begin(str(data.request_id), clarification_id=clarification_id, option_id=data.option_id)
    return await response_for(request, store, operation)


@router.get("/requests/{request_id}")
async def status(request: Request, request_id: UUID):
    """查询本 IP 快照，断线不重跑模型；演示已过期正文只返回 expired。"""
    snapshot = await store_for(request).request_status(str(request_id))
    if snapshot.get("turn"):
        snapshot["turn"] = public_turn(snapshot["turn"])
    return snapshot


@router.post("/requests/{request_id}/cancel")
async def cancel(request: Request, request_id: UUID):
    """跨进程取消标志由心跳读取；重复停止幂等，计次由已发布输出状态决定。"""
    public_origin(request, get_settings())
    return await store_for(request).cancel(str(request_id))
