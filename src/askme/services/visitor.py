"""匿名 IP 身份入口：FastAPI → visitor_id → Redis，永不接受前端指定 IP。

Uvicorn 必须只信任部署者明确指定的代理。应用只使用已经处理过的 request.client，
不再次读取 X-Forwarded-For，避免客户端伪造转发头绕过永久 20 次额度。
"""
import hashlib
import hmac
import ipaddress

from fastapi import Request

from askme.core.config import Settings
from askme.core.errors import AppError


def visitor_id(request: Request, settings: Settings) -> str:
    """规范化来源后返回 HMAC 标识；示例 ::ffff:192.0.2.1 与 192.0.2.1 同身份。

    原始 IP 只在函数局部存在；缺密钥/无有效来源时失败，不能给所有人默认共用身份。
    """
    secret = settings.ip_id_secret.get_secret_value()
    if len(secret) < 32:
        raise AppError("CHAT_NOT_CONFIGURED", "公开问答尚未配置，请联系站点管理员", 503)
    try:
        address = ipaddress.ip_address(request.client.host if request.client else "")
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
    except ValueError:
        raise AppError("INVALID_SOURCE", "无法识别请求来源", 400) from None
    return hmac.new(secret.encode(), address.compressed.encode(), hashlib.sha256).hexdigest()


def public_origin(request: Request, settings: Settings) -> None:
    """修改接口校验同源；演示 localhost:5174 可开发，恶意网页 Origin 被拒绝。

    生产域名需写入 ALLOWED_ORIGINS；不凭 Host 头动态放行。无 Origin 也拒绝。
    """
    allowed = settings.origins | {v.strip().rstrip("/") for v in settings.chat_allowed_origins.split(",") if v.strip()}
    if request.headers.get("origin", "").rstrip("/") not in allowed:
        raise AppError("ORIGIN_REJECTED", "请求来源不被允许", 403)
