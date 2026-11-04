"""从项目根目录读取配置，不依赖启动进程的工作目录。"""

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        # 示例中预留了后续业务配置；现阶段无需因这些未使用项而阻止启动。
        extra="ignore",
    )

    app_name: str = "cqyc-askme"
    app_env: str = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    admin_username: str = ""
    admin_password: SecretStr = SecretStr("")
    session_secret: SecretStr = SecretStr("")
    admin_session_ttl_seconds: int = Field(28800, ge=60)
    cookie_secure: bool = False
    allowed_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:8000,http://127.0.0.1:8000"
    redis_url: SecretStr = SecretStr("redis://localhost:6379/0")
    import_work_dir: Path = Path("data/imports")
    pdf_to_md_dir: Path = Path("docs/pdfToMd")
    upload_max_bytes: int = Field(20 * 1024 * 1024, gt=0)
    pdf_max_pages: int = Field(200, gt=0)
    mineru_base_url: str = "http://127.0.0.1:8001"
    mineru_api_key: SecretStr = SecretStr("")
    mineru_tier: str = "standard"
    mineru_timeout_seconds: int = Field(1800, ge=10)
    parser_max_bytes: int = Field(200 * 1024 * 1024, gt=0)
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: SecretStr = SecretStr("")
    deepseek_model: str = "deepseek-flash"
    embedding_base_url: str = "http://127.0.0.1:7997"
    embedding_api_key: SecretStr = SecretStr("")
    embedding_model: str = "BAAI/bge-m3"
    embedding_revision: str = "main"
    # .env 的值是字符串，先按 int 解析，再限制为 BGE-M3 的固定 1024 维。
    embedding_dimension: int = Field(1024, ge=1024, le=1024)
    tokenizer_path: str = "BAAI/bge-m3"
    tokenizer_local_only: bool = True
    embedding_batch_size: int = Field(16, ge=1, le=128)
    minio_endpoint: str = "127.0.0.1:9000"
    minio_access_key: SecretStr = SecretStr("")
    minio_secret_key: SecretStr = SecretStr("")
    minio_bucket: str = "askme-assets"
    minio_secure: bool = False
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: SecretStr = SecretStr("")
    milvus_collection: str = "askme_chunks_bge_m3_v1"
    chunk_size: int = Field(700, ge=32)
    chunk_min_size: int = Field(180, ge=1)
    chunk_max_size: int = Field(1000, le=8000)
    chunk_overlap: int = Field(80, ge=0)
    # 公开问答配置与管理员会话分离：IP 密钥轮换会改变匿名身份，不能随登录密钥更换。
    # 演示：历史 30 天到期后，永久 ledger 中 used=20 仍保留，不再次发放额度。
    ip_id_secret: SecretStr = SecretStr("")
    chat_allowed_origins: str = "http://localhost:5174,http://127.0.0.1:5174"
    chat_history_ttl: int = Field(2592000, ge=3600)
    chat_timeout: int = Field(120, ge=30, le=300)
    chat_prompt_budget: int = Field(24000, ge=18000)
    chat_output_tokens: int = Field(1200, ge=128, le=1200)
    rag_top_k: int = Field(20, ge=1, le=50)
    answer_context_top_k: int = Field(6, ge=1, le=10)
    rerank_base_url: str = ""
    rerank_api_key: SecretStr = SecretStr("")
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    rerank_min_score: float = Field(0.5, ge=0, le=1)
    bailian_search_mcp_url: str = ""
    bailian_search_tool_name: str = ""
    dashscope_api_key: SecretStr = SecretStr("")
    # 默认零预算：部署者核对百炼计费后填写，不能因上线代码自动产生搜索费用。
    web_search_budget: int = Field(0, ge=0)

    @model_validator(mode="after")
    def validate_limits(self) -> "Settings":
        if not self.chunk_min_size <= self.chunk_size <= self.chunk_max_size:
            raise ValueError("分块长度必须满足 min <= size <= max")
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError("chunk_overlap 必须小于 chunk_size")
        if self.app_env == "production" and (not self.cookie_secure or len(self.session_secret.get_secret_value()) < 32):
            raise ValueError("生产环境必须启用 COOKIE_SECURE 并配置至少 32 字符 SESSION_SECRET")
        return self

    def resolve(self, path: Path) -> Path:
        return (PROJECT_ROOT / path).resolve()

    @property
    def origins(self) -> set[str]:
        return {value.strip().rstrip("/") for value in self.allowed_origins.split(",") if value.strip()}


@lru_cache
def get_settings() -> Settings:
    """每个进程只加载一次配置，避免每次请求重复读取环境文件。"""
    return Settings()
