"""管理员认证、文件上传、任务查询、进度和受保护的产物读取。"""

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Query, Request, Response, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, Field, SecretStr
from pypdf import PdfReader
from redis.exceptions import RedisError

from askme.core.config import get_settings
from askme.core.errors import AppError
from askme.services.auth import COOKIE, current_session, login, require_admin, session_key
from askme.services.storage import TaskStore, atomic_json, check_id, fingerprint, read_json

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


def store(request: Request) -> TaskStore:
    return TaskStore(get_settings(), request.app.state.redis)


class LoginInput(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=512)


@router.post("/auth/login")
async def sign_in(body: LoginInput, request: Request, response: Response):
    token, session = await login(request, body.username, body.password.get_secret_value())
    settings = get_settings()
    response.set_cookie(COOKIE, token, max_age=settings.admin_session_ttl_seconds,
        httponly=True, secure=settings.cookie_secure, samesite="strict", path="/")
    return {"expires_at": session["expires_at"], "csrf_token": session["csrf_token"], "upload_max_bytes": settings.upload_max_bytes}


@router.get("/auth/me")
async def me(session: dict = Depends(require_admin)):
    return {"expires_at": session["expires_at"], "csrf_token": session["csrf_token"], "upload_max_bytes": get_settings().upload_max_bytes}


@router.post("/auth/logout")
async def sign_out(request: Request, response: Response, _: dict = Depends(require_admin)):
    await request.app.state.redis.delete(session_key(request.cookies.get(COOKIE, "")))
    response.delete_cookie(COOKIE, path="/", secure=get_settings().cookie_secure, httponly=True, samesite="strict")
    logger.info("Admin logged out request_id=%s", request.state.request_id)
    return {"ok": True}


def validate_file(path: Path, suffix: str) -> None:
    if suffix == ".md":
        try:
            text = path.read_text(encoding="utf-8-sig")
        except UnicodeError:
            raise AppError("INVALID_ENCODING", "Markdown 必须是 UTF-8 编码", 422) from None
        if not text.strip() or any(ord(c) < 32 and c not in "\n\r\t" for c in text):
            raise AppError("INVALID_MARKDOWN", "Markdown 为空或包含二进制内容", 422)
        return
    try:
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise AppError("INVALID_PDF", "文件内容不是 PDF", 422)
        pdf = PdfReader(path)
        if pdf.is_encrypted:
            raise AppError("ENCRYPTED_PDF", "请先移除 PDF 密码后再上传", 422)
        pages = len(pdf.pages)
        if not pages or pages > get_settings().pdf_max_pages:
            raise AppError("PDF_PAGE_LIMIT", f"PDF 页数应为 1–{get_settings().pdf_max_pages}", 422)
    except AppError:
        raise
    except Exception:
        raise AppError("INVALID_PDF", "PDF 文件损坏或无法读取", 422) from None


@router.post("/imports")
async def upload(request: Request, file: UploadFile = File(...), _: dict = Depends(require_admin)):
    settings, tasks = get_settings(), store(request)
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in {".pdf", ".md"}:
        raise AppError("UNSUPPORTED_FILE", "只支持 PDF 和 Markdown 文件", 415)
    task_id = uuid.uuid4().hex
    folder = tasks.folder(task_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"source{suffix}"
    digest, size, accepted = hashlib.sha256(), 0, False
    lock_key, lock_token = "", uuid.uuid4().hex
    try:
        with path.open("wb") as output:
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > settings.upload_max_bytes:
                    raise AppError("FILE_TOO_LARGE", "文件超过上传大小限制", 413)
                digest.update(block)
                output.write(block)
            output.flush()
            os.fsync(output.fileno())
        if not size:
            raise AppError("EMPTY_FILE", "不能上传空文件", 422)
        await asyncio.to_thread(validate_file, path, suffix)
        dedup_key = f"askme:dedup:{digest.hexdigest()}:{suffix}:{fingerprint(settings)}"
        lock_key = f"{dedup_key}:lock"
        if not await tasks.redis.set(lock_key, lock_token, nx=True, ex=60):
            raise AppError("IMPORT_BUSY", "相同文件正在提交，请稍后刷新任务列表", 409, True)
        existing = await tasks.redis.get(dedup_key)
        if existing:
            previous = await tasks.get(existing)
            if previous["status"] == "queued" and not previous.get("event_id"):
                await tasks.save(previous, "恢复尚未确认入队的任务", enqueue=True)
            return JSONResponse({**previous, "reused": True}, status_code=202 if previous["status"] in {"queued", "running"} else 200)
        filename = Path((file.filename or "document").replace("\\", "/")).name[:160]
        filename = re.sub(r"[\x00-\x1f\x7f]", "", filename)
        task = tasks.new(task_id, suffix, filename, digest.hexdigest(), size)
        task["dedup_key"] = dedup_key
        # 先保存恢复清单；入队和去重登记通过同一锁保护，网络故障时保留可对账记录。
        atomic_json(folder / "manifest.json", task)
        accepted = True
        await tasks.redis.set(dedup_key, task_id)
        await tasks.save(task, "文件已接收，等待处理", enqueue=True)
        logger.info("Import accepted task_id=%s bytes=%s type=%s", task_id, size, suffix)
        return JSONResponse({**task, "reused": False}, status_code=202)
    finally:
        await file.close()
        if lock_key:
            try:
                await tasks.redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, lock_key, lock_token)
            except RedisError:
                logger.warning("Import lock release deferred task_id=%s", task_id)
        if not accepted:
            shutil.rmtree(folder, ignore_errors=True)


@router.get("/imports")
async def list_imports(request: Request, offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100), _: dict = Depends(require_admin)):
    return await store(request).recent(offset, limit)


@router.get("/imports/{task_id}")
async def detail(task_id: str, request: Request, _: dict = Depends(require_admin)):
    return await store(request).get(task_id)


@router.post("/imports/{task_id}/retry")
async def retry_task(task_id: str, request: Request, _: dict = Depends(require_admin)):
    tasks, token = store(request), uuid.uuid4().hex
    key = f"askme:retry:{check_id(task_id)}"
    if not await tasks.redis.set(key, token, nx=True, ex=30):
        raise AppError("RETRY_BUSY", "任务正在重试", 409)
    try:
        task = await tasks.get(task_id)
        if task["status"] not in {"failed", "interrupted"}:
            raise AppError("NOT_RETRYABLE", "仅失败或中断任务可重试", 409)
        if task["config_fingerprint"] != fingerprint(get_settings()):
            raise AppError("CONFIG_CHANGED", "处理配置已变更，请重新上传以创建新版本", 409)
        task.setdefault("attempt_errors", []).append({"attempt": task["attempt"], "error": task["error"]})
        task.update(status="queued", error=None, attempt=task["attempt"] + 1)
        await tasks.save(task, "重新排队，保留已完成阶段", enqueue=True)
        logger.info("Import retry queued task_id=%s attempt=%s", task_id, task["attempt"])
        return JSONResponse(task, status_code=202)
    finally:
        await tasks.redis.eval("if redis.call('GET',KEYS[1])==ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0", 1, key, token)


@router.get("/imports/{task_id}/events")
async def events(task_id: str, request: Request, _: dict = Depends(require_admin)):
    tasks = store(request)
    snapshot = await tasks.get(task_id)

    async def stream():
        cursor = request.headers.get("last-event-id", "")
        if not re.fullmatch(r"\d+-\d+", cursor):
            cursor = ""
        # 每次连接先发权威快照；浏览器无需依靠丢失的历史事件重建状态。
        cursor = snapshot.get("event_id", cursor or "0-0")
        yield f"id: {cursor}\ndata: {json.dumps(snapshot, ensure_ascii=False)}\n\n"
        while not await request.is_disconnected():
            try:
                await current_session(request)
                rows = await tasks.redis.xread({f"askme:events:{task_id}": cursor}, count=50, block=5000)
                if not rows:
                    yield ": heartbeat\n\n"
                for _, records in rows:
                    for event_id, fields in records:
                        cursor = event_id
                        yield f"id: {event_id}\ndata: {fields['data']}\n\n"
                latest = await tasks.get(task_id)
                if latest["status"] in {"succeeded", "failed", "interrupted"}:
                    yield f"id: {latest.get('event_id', cursor)}\ndata: {json.dumps(latest, ensure_ascii=False)}\n\n"
                    return
            except (AppError, RedisError):
                yield 'event: unavailable\ndata: {}\n\n'
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no", "Cache-Control": "no-store"})


@router.get("/imports/{task_id}/markdown")
async def markdown(task_id: str, request: Request, variant: str = Query("enriched", pattern="^(original|enriched)$"), _: dict = Depends(require_admin)):
    tasks = store(request)
    task = await tasks.get(task_id)
    path = tasks.output(task) / f"{variant}.md"
    if not path.exists():
        raise AppError("ARTIFACT_NOT_READY", "该 Markdown 尚未生成", 409)
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@router.get("/assets/{asset_id}")
async def asset(asset_id: str, request: Request, _: dict = Depends(require_admin)):
    # asset_id 是 task_id + 图片哈希，所有对象键均从本地可信清单读取。
    parts = asset_id.split("-")
    if len(parts) != 2 or not re.fullmatch(r"[a-f0-9]{64}", parts[-1]):
        raise AppError("NOT_FOUND", "图片不存在", 404)
    tasks = store(request)
    folder = tasks.folder(parts[0])
    if not (folder / "assets.json").exists():
        raise AppError("NOT_FOUND", "图片不存在", 404)
    assets = read_json(folder / "assets.json")
    item = next((value for value in assets if value["asset_id"] == asset_id), None)
    if not item:
        raise AppError("NOT_FOUND", "图片不存在", 404)
    from askme.integrations.clients import ObjectStore
    data = await asyncio.to_thread(ObjectStore(get_settings()).read, item["object_key"])
    return Response(data, media_type=item["mime"], headers={"X-Content-Type-Options": "nosniff", "Content-Security-Policy": "default-src 'none'"})
