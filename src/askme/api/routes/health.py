"""不访问外部依赖的进程存活接口。"""

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/health")
async def health() -> dict[str, str]:
    # 此状态仅表示 HTTP 进程存活；外部依赖就绪检查在集成阶段单独设计。
    return {"status": "ok"}
