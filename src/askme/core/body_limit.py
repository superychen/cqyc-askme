"""在 multipart 解析前限制原始请求体，涵盖无 Content-Length 的分块上传。"""

from starlette.responses import JSONResponse


class BodyLimitExceeded(Exception):
    pass


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int):
        self.app, self.max_bytes = app, max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        size, exceeded, started = 0, False, False

        async def limited_receive():
            nonlocal size, exceeded
            message = await receive()
            if message["type"] == "http.request":
                size += len(message.get("body", b""))
                if size > self.max_bytes:
                    exceeded = True
                    raise BodyLimitExceeded()
            return message

        async def guarded_send(message):
            nonlocal started
            # 解析器可能把读取失败转换为 400；超限时抑制该响应，统一返回 413。
            if exceeded:
                return
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, guarded_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not started:
            response = JSONResponse({"code": "FILE_TOO_LARGE", "message": "请求体超过大小限制",
                "request_id": scope.get("state", {}).get("request_id", "unknown")}, status_code=413)
            await response(scope, receive, send)
