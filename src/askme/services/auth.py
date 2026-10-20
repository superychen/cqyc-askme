"""单管理员登录：Redis 会话、凭据变更失效及 CSRF 防护。"""

import hashlib
import hmac
import json
import logging
import secrets
import time

from fastapi import Request

from askme.core.config import get_settings
from askme.core.errors import AppError

COOKIE = "askme_admin"
logger = logging.getLogger(__name__)


def credentials_fingerprint() -> str:
    settings = get_settings()
    return hmac.new(settings.session_secret.get_secret_value().encode(),
        f"{settings.admin_username}\0{settings.admin_password.get_secret_value()}".encode(), hashlib.sha256).hexdigest()


def session_key(token: str) -> str:
    return "askme:session:" + hashlib.sha256(token.encode()).hexdigest()


def check_origin(request: Request) -> None:
    origin = request.headers.get("origin", "").rstrip("/")
    if origin not in get_settings().origins:
        raise AppError("ORIGIN_REJECTED", "请求来源不被允许", 403)


async def current_session(request: Request) -> dict:
    token = request.cookies.get(COOKIE, "")
    if not token or len(token) > 256:
        raise AppError("UNAUTHORIZED", "请先登录", 401)
    raw = await request.app.state.redis.get(session_key(token))
    session = json.loads(raw) if raw else None
    if not session or not hmac.compare_digest(session["fingerprint"], credentials_fingerprint()):
        raise AppError("UNAUTHORIZED", "登录已失效，请重新登录", 401)
    return session


async def require_admin(request: Request) -> dict:
    session = await current_session(request)
    if request.method not in {"GET", "HEAD", "OPTIONS"}:
        check_origin(request)
        if not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf_token"]):
            raise AppError("CSRF_REJECTED", "操作校验失败，请刷新页面", 403)
    return session


async def login(request: Request, username: str, password: str) -> tuple[str, dict]:
    check_origin(request)
    settings = get_settings()
    if not settings.admin_username or not settings.admin_password.get_secret_value() or len(settings.session_secret.get_secret_value()) < 32:
        raise AppError("ADMIN_NOT_CONFIGURED", "管理员尚未配置，请设置账号、密码和会话密钥", 503)
    # 来源仅用 HMAC 摘要作为限流键，不把客户端 IP 或账户信息写入日志。
    source = request.client.host if request.client else "unknown"
    key = "askme:login:" + hmac.new(settings.session_secret.get_secret_value().encode(), source.encode(), hashlib.sha256).hexdigest()
    attempts = await request.app.state.redis.eval("local n=redis.call('INCR',KEYS[1]); if n==1 then redis.call('EXPIRE',KEYS[1],300) end; return n", 1, key)
    if attempts > 10:
        raise AppError("RATE_LIMITED", "尝试次数过多，请 5 分钟后重试", 429)
    account_ok = hmac.compare_digest(username.encode(), settings.admin_username.encode())
    password_ok = hmac.compare_digest(password.encode(), settings.admin_password.get_secret_value().encode())
    if not (account_ok and password_ok):
        logger.warning("Admin login rejected request_id=%s", request.state.request_id)
        raise AppError("INVALID_CREDENTIALS", "账号或密码错误", 401)
    token = secrets.token_urlsafe(48)
    session = {"expires_at": int(time.time()) + settings.admin_session_ttl_seconds,
        "csrf_token": secrets.token_urlsafe(32), "fingerprint": credentials_fingerprint()}
    pipe = request.app.state.redis.pipeline(transaction=True)
    pipe.delete(key)
    old = request.cookies.get(COOKIE)
    if old:
        pipe.delete(session_key(old))
    pipe.set(session_key(token), json.dumps(session), ex=settings.admin_session_ttl_seconds)
    await pipe.execute()
    logger.info("Admin login succeeded request_id=%s", request.state.request_id)
    return token, session
