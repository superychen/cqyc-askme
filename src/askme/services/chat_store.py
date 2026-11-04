"""Redis 原子会话：路由 begin → 图输出 publish → finish → 历史/摘要。

永久 ledger 仅保存计数、至多 40 个已扣次请求/续接回执和活动租约；正文放有 TTL 的
history。用 Lua 比较并交换两份 JSON，避免先查再扣的竞态。演示 used=19 时两个并发
请求只有一个持有租约；它产生首个输出后 used=20，第 21 个新问题被拒绝。
"""
import hashlib
import json
import logging
import time
import uuid

from askme.core.errors import AppError

logger = logging.getLogger(__name__)
PREFIX = "askme:public:"
LIMIT = 20
LEASE = 45
# Lua在同一原子操作内比较两份快照、检查全局8个有效租约并写回；不能先count再加锁。
# expired成员仍留在索引等待回收，但ZCOUNT只计未来到期者；同IP续约不重复占容量。
CAS = """
if (redis.call('GET',KEYS[1]) or '') ~= ARGV[1] or (redis.call('GET',KEYS[2]) or '') ~= ARGV[2] then return 0 end
if tonumber(ARGV[7]) > 0 and tonumber(redis.call('ZSCORE',KEYS[3],ARGV[8]) or '0') <= tonumber(ARGV[9]) and redis.call('ZCOUNT',KEYS[3],ARGV[9],'+inf') >= 8 then return -1 end
redis.call('SET',KEYS[1],ARGV[3])
if ARGV[5] == '1' then redis.call('SET',KEYS[2],ARGV[4],'EX',ARGV[6])
elseif redis.call('EXISTS',KEYS[2]) == 1 then redis.call('SET',KEYS[2],ARGV[4],'KEEPTTL') end
if tonumber(ARGV[7]) > 0 then redis.call('ZADD',KEYS[3],ARGV[7],ARGV[8]) else redis.call('ZREM',KEYS[3],ARGV[8]) end
return 1
"""


def encode(value: dict) -> str:
    """稳定 JSON 便于 CAS；演示中文原样编码，禁止 NaN 破坏 Redis/Lua 协议。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ChatStore:
    """每请求轻量对象，客户端由 lifespan 复用；ip_id 已经 HMAC，不含原始地址。"""
    def __init__(self, redis, ip_id: str, ttl: int):
        """绑定本 IP 的两个 key；示例不同 IP 的 CAS 互不争用。"""
        self.redis, self.ip_id, self.ttl = redis, ip_id, ttl
        self.keys = [PREFIX + "ledger:" + ip_id, PREFIX + "history:" + ip_id, PREFIX + "active"]

    async def mutate(self, operation, *, touch: bool = False):
        """读两份快照→纯内存变更→CAS，冲突最多 12 次后返回忙碌，不丢别人的写入。

        operation 同步返回业务结果；成功 CAS 前不执行外呼或发 SSE，故重试无副作用。
        touch 仅有效对话活动延长历史 TTL，心跳/查询不延长；ledger 永远不设 TTL。
        """
        for _ in range(12):
            raw, body = await self.redis.mget(self.keys[:2])
            ledger = json.loads(raw) if raw else {"used": 0, "receipts": {}, "active": None}
            ledger.setdefault("first_seen_at", time.time())
            ledger["last_seen_at"] = time.time()
            history = json.loads(body) if body else {"turns": [], "summary": "", "covered": [], "version": 0}
            # 失效租约由所有入口和 sweeper 共同回收；charged 已在 ledger 扣次，不能退款。
            active = ledger.get("active")
            if active and active["expires"] <= time.time():
                for turn in history["turns"]:
                    if turn["id"] == active["turn_id"]:
                        turn["status"] = "interrupted" if active["charged"] else "failed"
                        turn["error"] = "请求已中断，请重新提问"
                if active["request_id"] in ledger["receipts"]:
                    ledger["receipts"][active["request_id"]]["status"] = "interrupted"
                ledger["active"] = None
            result = operation(ledger, history)
            active = ledger.get("active")
            ok = await self.redis.eval(CAS, 3, *self.keys, raw or "", body or "", encode(ledger), encode(history),
                                       "1" if touch else "0", self.ttl, active["expires"] if active else 0, self.ip_id, time.time())
            if ok == -1:
                raise AppError("CHAT_CAPACITY", "当前访问较多，请稍后重试", 503, True)
            if ok == 1:
                return result
        raise AppError("CHAT_BUSY", "当前网络有其他操作正在处理，请稍后重试", 409, True)

    async def snapshot(self) -> dict:
        """返回本 IP 内容和额度，顺便回收崩溃租约；演示历史空而 used=20，remaining=0。"""
        def read(ledger, history):
            """扣除未发布请求的预占；演示used=19且预占1，前端remaining=0。"""
            active = ledger.get("active")
            return {**history, "used": ledger["used"], "remaining": max(0, LIMIT-ledger["used"]-int(bool(active and not active["charged"]))),
                    "active_request": active["request_id"] if active else None}
        return await self.mutate(read)

    async def begin(self, request_id: str, message: str = "", clarification_id: str = "", option_id: str = "") -> dict:
        """原子查重、澄清消费、额度预占和加锁，返回 owner/turn/question 或 duplicate。

        演示：第 20 问的澄清已 charged，选择合法选项不再加 used；伪造文本/选项拒绝。
        未输出就失败仅释放租约（即预占）；同 UUID 不同正文返回 409。
        """
        digest = hashlib.sha256(encode({"message": message, "clarification": clarification_id, "option": option_id}).encode()).hexdigest()
        owner = uuid.uuid4().hex
        def accept(ledger, history):
            """CAS闭包只修改内存副本；每次冲突重新核对请求哈希、选项和租约。"""
            active = ledger.get("active")
            old = ledger["receipts"].get(request_id)
            if not old:
                old = next((r for t in history["turns"] for r in t.get("requests", []) if r["request_id"] == request_id), None)
            if old:
                if old["digest"] != digest:
                    raise AppError("REQUEST_CONFLICT", "请求标识已被其他内容使用", 409)
                return {"duplicate": True, "request_id": request_id}
            if active:
                raise AppError("CHAT_BUSY", "当前网络正在生成回答，请稍后再试", 409, True)
            continuation = bool(clarification_id)
            if continuation:
                turn = next((t for t in history["turns"] if t.get("clarification", {}).get("id") == clarification_id), None)
                card = turn.get("clarification") if turn else None
                if not card or card["expires_at"] <= time.time() or card.get("consumed"):
                    raise AppError("CLARIFICATION_EXPIRED", "该选项已失效，请重新输入问题", 409)
                option = next((v for v in card["options"] if v["id"] == option_id and v["action"] == "ask"), None)
                if not option:
                    raise AppError("INVALID_OPTION", "请选择有效的问题选项", 422)
                card["consumed"] = True
                question = option["label"]
                turn.update(selection=question, answer="", citations=[], status="running")
            else:
                if ledger["used"] >= LIMIT:
                    raise AppError("QUOTA_EXHAUSTED", "感谢你的关注，当前网络的 20 次体验提问已用完，欢迎通过简历中的联系方式继续交流。", 429)
                question = message
                # 新问题使旧卡片失效，防止历史变化后恢复一个语境过期的免费续接。
                for previous in history["turns"]:
                    if previous.get("clarification"):
                        previous["clarification"]["consumed"] = True
                turn = {"id": uuid.uuid4().hex, "question": question, "answer": "", "citations": [], "status": "running", "created_at": time.time(), "requests": []}
                history["turns"].append(turn)
                # 失败请求不消耗总额度，但正文记录也必须有容量上限。
                history["turns"] = history["turns"][-60:]
            receipt = {"request_id": request_id, "digest": digest, "turn_id": turn["id"], "status": "running"}
            turn["requests"].append(receipt)
            if continuation:
                ledger["receipts"][request_id] = receipt.copy()
            ledger["active"] = {**receipt, "owner": owner, "expires": time.time()+LEASE, "charged": continuation, "cancel": False}
            history["version"] += 1
            return {"owner": owner, "turn_id": turn["id"], "question": question, "continuation": continuation, "request_id": request_id, "duplicate": False}
        result = await self.mutate(accept, touch=True)
        logger.info("Chat accepted request_id=%s duplicate=%s", request_id, result["duplicate"])
        return result

    @staticmethod
    def owned(ledger, history, owner):
        """核对租约所有权后返回活动请求和 turn；迟到任务不能写入下一轮。"""
        active = ledger.get("active")
        if not active or active["owner"] != owner:
            raise AppError("CHAT_LEASE_LOST", "请求已中断", 409)
        turn = next((t for t in history["turns"] if t["id"] == active["turn_id"]), None)
        if not turn:
            raise AppError("CHAT_HISTORY_LOST", "会话状态已失效", 409)
        return active, turn

    async def heartbeat(self, owner: str) -> bool:
        """续约并读取跨进程取消信号；演示 cancel=true 返回 False，由协调器取消图。"""
        def renew(ledger, history):
            """仅owner匹配才能延期；cancel标志只读取，不清除别人发来的停止请求。"""
            active, _ = self.owned(ledger, history, owner)
            active["expires"] = time.time()+LEASE
            return not active["cancel"]
        return await self.mutate(renew)

    async def publish(self, owner: str, *, delta: str = "", clarification: dict | None = None):
        """先持久化可见输出再发 SSE；首段或选项仅扣一次，断线后可恢复。

        演示两次 delta='我'、'参与过…'，used 只从 19→20 一次，answer 累积。
        """
        def save(ledger, history):
            """首个输出先转charged再追加正文；重复delta不重复扣次，但由调用方保证不重发。"""
            active, turn = self.owned(ledger, history, owner)
            if active["cancel"]:
                raise AppError("CHAT_CANCELLED", "回答已停止", 409)
            if not active["charged"]:
                ledger["used"] += 1
                active["charged"] = True
                ledger["receipts"][active["request_id"]] = {k: active[k] for k in ("request_id", "digest", "turn_id", "status")}
            turn["answer"] += delta
            if clarification:
                turn["clarification"] = clarification
            return LIMIT-ledger["used"]
        return await self.mutate(save, touch=True)

    async def finish(self, owner: str, status: str, citations=None, error: str = ""):
        """提交终态并释放锁；无输出的失败不计次，有输出的失败标记 interrupted。"""
        def complete(ledger, history):
            """原子保存turn终态及回执，然后释放active；不能先释放锁再写旧回答。"""
            active, turn = self.owned(ledger, history, owner)
            actual = "interrupted" if status == "failed" and active["charged"] else status
            turn.update(status=actual, citations=citations or [], error=error)
            for receipt in turn["requests"]:
                if receipt["request_id"] == active["request_id"]:
                    receipt["status"] = actual
            if active["request_id"] in ledger["receipts"]:
                ledger["receipts"][active["request_id"]]["status"] = actual
            ledger["active"] = None
            history["version"] += 1
            return {"status": actual, "remaining": LIMIT-ledger["used"]}
        return await self.mutate(complete, touch=True)

    async def cancel(self, request_id: str):
        """仅取消本 IP 的指定活动请求；终态重复取消无副作用，不暴露别人的记录。"""
        def mark(ledger, history):
            """找不到同ID活动请求也返回accepted，避免取消接口暴露其他身份的记录。"""
            if ledger.get("active") and ledger["active"]["request_id"] == request_id:
                ledger["active"]["cancel"] = True
            return {"accepted": True}
        return await self.mutate(mark)

    async def request_status(self, request_id: str):
        """断线后找对应 turn；正文过期但永久回执在，返回 expired 而非再次执行。"""
        def read(ledger, history):
            """优先返回仍在TTL内的正文，只有永久回执时明确expired。"""
            for turn in reversed(history["turns"]):
                if any(r["request_id"] == request_id for r in turn["requests"]):
                    return {"turn": turn, "status": turn["status"], "remaining": LIMIT-ledger["used"]}
            if request_id in ledger["receipts"]:
                return {"turn": None, "status": "expired", "remaining": LIMIT-ledger["used"]}
            raise AppError("REQUEST_NOT_FOUND", "未找到该请求", 404)
        return await self.mutate(read)

    async def save_summary(self, version: int, summary: str, covered: list[str]):
        """CAS 保存摘要覆盖范围；版本改变说明有新问答，丢弃迟到摘要。演示 3→4 不覆盖。"""
        def save(ledger, history):
            """新一轮开始会增加version；旧摘要只允许写入自己读取的历史版本。"""
            if history["version"] != version:
                return False
            history.update(summary=summary, covered=covered)
            return True
        return await self.mutate(save)
