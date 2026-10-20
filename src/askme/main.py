"""FastAPI 应用入口；当前只提供进程存活检查。"""

import logging
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from redis.exceptions import RedisError

from askme.api.router import api_router
from askme.core.config import get_settings
from askme.core.errors import AppError, error_location
from askme.core.body_limit import BodyLimitMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # 只记录非敏感的应用标识，不输出完整配置或预留的账号、密钥等信息。
    logger.info("Application starting app=%s env=%s", settings.app_name, settings.app_env)
    missing = [name for name, value in (("ADMIN_USERNAME", settings.admin_username),
        ("ADMIN_PASSWORD", settings.admin_password.get_secret_value()),
        ("SESSION_SECRET", settings.session_secret.get_secret_value())) if not value]
    if missing:
        logger.warning("Admin configuration missing fields=%s", ",".join(missing))
    app.state.redis = Redis.from_url(settings.redis_url.get_secret_value(), decode_responses=True,
        socket_connect_timeout=3, socket_timeout=10)
    try:
        yield
    finally:
        await app.state.redis.aclose()
        logger.info("Application stopped app=%s", settings.app_name)


def create_app() -> FastAPI:
    """外部服务按请求延迟连接，未配置时仍可查看健康状态和登录页面。"""
    app = FastAPI(title=get_settings().app_name, version="0.1.0", lifespan=lifespan)
    app.include_router(api_router, prefix="/api/v1")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        request.state.request_id = uuid.uuid4().hex
        # 提前拒绝明显超限的 multipart，文件流本身仍在上传接口二次计数。
        length = request.headers.get("content-length", "0")
        if request.method == "POST" and length.isdigit() and int(length) > get_settings().upload_max_bytes + 2 * 1024 * 1024:
            response = JSONResponse({"code": "FILE_TOO_LARGE", "message": "请求体超过大小限制", "request_id": request.state.request_id}, status_code=413)
        else:
            response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        if request.url.path.startswith("/api/v1/admin"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        return JSONResponse({"code": exc.code, "message": exc.message, "retryable": exc.retryable,
            "request_id": request.state.request_id}, status_code=exc.status)

    @app.exception_handler(RedisError)
    async def redis_error(request: Request, exc: RedisError):
        logger.warning("Redis unavailable request_id=%s error_type=%s", request.state.request_id, type(exc).__name__)
        return JSONResponse({"code": "REDIS_UNAVAILABLE", "message": "缓存或任务服务不可用，请检查 Redis 配置", "request_id": request.state.request_id}, status_code=503)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError):
        # 默认验证错误可能包含用户提交的密码；这里只返回字段位置和错误类型。
        return JSONResponse({"code": "INVALID_REQUEST", "message": "请求参数不正确", "request_id": request.state.request_id,
            "fields": [{"loc": list(error["loc"]), "type": error["type"]} for error in exc.errors()]}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        logger.error("Request failed request_id=%s error_type=%s locations=%s", getattr(request.state, "request_id", "unknown"), type(exc).__name__, error_location(exc))
        return JSONResponse({"code": "INTERNAL_ERROR", "message": "操作失败，请根据请求编号查看日志", "request_id": getattr(request.state, "request_id", "unknown")}, status_code=500)
    app.add_middleware(BodyLimitMiddleware, max_bytes=get_settings().upload_max_bytes + 2 * 1024 * 1024)
    return app


app = create_app()
