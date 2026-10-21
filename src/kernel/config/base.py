"""Settings class definition."""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from typing import TYPE_CHECKING, Any, Optional

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings

from src.infra.logging import get_logger

from .constants import JWT_SECRET_KEY_MIN_LENGTH, MCP_ENCRYPTION_SALT_MIN_LENGTH
from .utils import (
    COMMIT_HASH,
    GIT_TAG,
    PROJECT_ROOT,
    expand_encryption_salt,
    expand_jwt_secret_key,
    get_app_version,
)

if TYPE_CHECKING:
    from src.infra.storage.s3 import S3Config

logger = get_logger(__name__)

class Settings(BaseSettings):
    """
    Application settings loaded from environment variables.

    Default values are defined in SETTING_DEFINITIONS (single source of truth).
    This class uses pydantic-settings to load from .env and environment variables.
    Runtime values can be updated from database via initialize_settings().
    """

    # Application (not in SETTING_DEFINITIONS - internal use only)
    APP_NAME: str = "昆小智"
    APP_VERSION: str = Field(default_factory=get_app_version)

    # Version Info (populated at startup)
    GIT_TAG: Optional[str] = None
    COMMIT_HASH: Optional[str] = None
    BUILD_TIME: Optional[str] = None

    # Debug (not in SETTING_DEFINITIONS - developer toggle)
    DEBUG_STREAM_EVENTS: bool = False

    # Logging Configuration (not in SETTING_DEFINITIONS - internal use only)
    LOG_LEVELS: str = ""
    LOG_FORMAT: str = (
        "%(asctime)s.%(msecs)03d [%(levelname)s] %(trace_context)s%(name)s - %(message)s"
    )
    LOG_DATE_FORMAT: str = "%Y-%m-%d %H:%M:%S"

    # Session Configuration (not in SETTING_DEFINITIONS)
    SESSION_MAX_MESSAGES: int = 20
    SESSION_MAX_EVENTS_PER_TRACE: int = 10000  # 单个 trace 最多保留的事件数，防止内存爆炸
    SESSION_EVENT_READ_DEFAULT_LIMIT: int = 1000
    SESSION_EVENT_MONGO_BUFFER_MAX: int = 10000
    SESSION_EVENT_TTL_CACHE_MAX: int = 5000
    SESSION_EVENT_REDIS_REPLAY_BATCH_SIZE: int = 500
    # Immutable trace event rollout.  ``legacy`` preserves the pre-migration
    # array reader/writer; ``dual`` writes both stores and reads can be merged;
    # ``event_store`` makes trace_events authoritative.
    TRACE_EVENT_WRITE_MODE: str = "legacy"
    TRACE_EVENT_READ_MODE: str = "legacy"
    TRACE_EVENT_BACKFILL_ENABLED: bool = False
    MONGODB_TRACE_EVENTS_COLLECTION: str = "trace_events"
    # Stale trace recovery runs under the startup cleanup lease. It is
    # deliberately opt-out and bounded so rollback is a single config change.
    TRACE_STALE_RECOVERY_ENABLED: bool = True
    TRACE_STALE_RECOVERY_GRACE_SECONDS: int = Field(default=120, gt=0)
    TRACE_STALE_RECOVERY_BATCH_SIZE: int = Field(default=100, gt=0)

    @field_validator("TRACE_EVENT_WRITE_MODE", mode="before")
    @classmethod
    def validate_trace_event_write_mode(cls, value: Any) -> str:
        normalized = str(value or "legacy").strip().lower()
        allowed = {"legacy", "dual", "event_store"}
        if normalized not in allowed:
            raise ValueError(
                f"trace event write mode must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalized

    @field_validator("TRACE_EVENT_READ_MODE", mode="before")
    @classmethod
    def validate_trace_event_read_mode(cls, value: Any) -> str:
        normalized = str(value or "legacy").strip().lower()
        allowed = {"legacy", "merge", "event_store"}
        if normalized not in allowed:
            raise ValueError(
                f"trace event read mode must be one of {sorted(allowed)}, got {value!r}"
            )
        return normalized

    @field_validator("MONGODB_TRACE_EVENTS_COLLECTION", mode="before")
    @classmethod
    def validate_trace_event_collection(cls, value: Any) -> str:
        normalized = str(value or "").strip()
        if (
            not normalized
            or normalized.startswith("$")
            or "\x00" in normalized
            or normalized.startswith("system.")
        ):
            raise ValueError("MONGODB_TRACE_EVENTS_COLLECTION must be a simple collection name")
        return normalized
    # ============================================
    # All settings below get defaults from SETTING_DEFINITIONS
    # ============================================

    # Application Settings
    DEBUG: bool = False
    HOST: str = "0.0.0.0"
    PORT: int = 8000
    APP_BASE_URL: str = ""  # e.g. https://kunxiaozhi.example.com — 用于生成文件 URL 的固定前缀
    LOG_LEVEL: str = "INFO"

    # LLM Settings
    LLM_MAX_RETRIES: int = 3
    LLM_RETRY_DELAY: float = 1.0
    LLM_MODEL_CACHE_SIZE: int = 50  # 模型实例缓存大小，防止内存泄漏
    PROMPT_CACHE_MAX_SYSTEM_BLOCKS: int = 4
    PROMPT_CACHE_MAX_TOOLS: int = 1

    # MCP Settings
    ENABLE_MCP: bool = True
    ENABLE_DEFERRED_TOOL_LOADING: bool = True
    DEFERRED_TOOL_THRESHOLD: int = 20
    DEFERRED_TOOL_SEARCH_LIMIT: int = 25
    DEFERRED_TOOL_PROMPT_LIMIT: int = 25
    MCP_GLOBAL_CACHE_TTL_SECONDS: int = 900
    MCP_GLOBAL_MAX_ENTRIES: int = 100
    MCP_GLOBAL_INIT_WAIT_SECONDS: int = 5
    MCP_GLOBAL_WARMUP_CONCURRENCY: int = 5
    MCP_GLOBAL_WARMUP_MAX_USERS: int = 100
    MCP_USER_CACHE_TTL_SECONDS: int = 900
    MCP_USER_CACHE_MAX_ENTRIES: int = 100
    MCP_POOL_TTL_SECONDS: int = 900
    MCP_POOL_MAX_CONNECTIONS: int = 100
    MCP_SERVER_LOAD_CONCURRENCY: int = 4
    MCP_EFFECTIVE_CONFIG_MAX_SERVERS: int = 100
    MCP_EFFECTIVE_CONFIG_MAX_TOOLS: int = 200
    MCP_ENCRYPTION_SALT: Optional[str] = None  # 默认随机生成，确保加密一致性
    DEEPAGENT_DEFAULT_MAX_INPUT_TOKENS: int = 64000

    # Session Settings
    SESSION_MAX_RUNS_PER_SESSION: int = 100
    ENABLE_MESSAGE_HISTORY: bool = True
    SSE_CACHE_TTL: int = 86400
    SESSION_SEARCH_BACKFILL_STARTUP_DELAY_SECONDS: float = 30.0
    SESSION_TITLE_MODEL_ID: str = ""
    SESSION_TITLE_PROMPT: str = "请根据用户消息生成一个简洁、准确的会话标题。\n\n# 要求\n\n1. 请务必使用{lang}回复\n2. 标题长度控制在5-8个字\n3. 只返回标题文本，不要添加Emoji、表情符号、引号或其他特殊格式\n\n用户消息：{message}"
    ENABLE_RECOMMEND_QUESTIONS: bool = True
    RECOMMEND_QUESTIONS_MAX_BACKGROUND_TASKS: int = 8

    # Team Agent SOP Settings
    TEAM_SOP_MODE: bool = False  # SOP opt-in; legacy team routing is the default.
    TEAM_SOP_MAX_STEPS: int = 8  # SOP 计划步骤数上限，超限工具拒绝并提示合并
    TEAM_SOP_MIN_STEPS: int = 2  # 步骤数下限，低于提示直接回答

    # Redis Settings
    REDIS_URL: str = "redis://localhost:6379/0"
    REDIS_PASSWORD: Optional[str] = None
    REDIS_SENTINEL_HOSTS: str = ""
    REDIS_SENTINEL_MASTER: str = ""
    REDIS_SENTINEL_PASSWORD: Optional[str] = None

    # Task execution settings
    TASK_BACKEND: str = "arq"  # local | arq
    ARQ_EMBEDDED_WORKER: bool = True
    ARQ_QUEUE_NAME: str = "kunxiaozhi:arq"
    ARQ_WORKER_MAX_JOBS: int = 64
    ARQ_JOB_TIMEOUT_SECONDS: int = 86400
    TASK_STARTUP_CLEANUP_CONCURRENCY: int = 16
    WECOM_RUNTIME_MODE: str = "embedded"  # embedded | external | disabled

    # MongoDB Settings
    MONGODB_URL: str = "mongodb://localhost:27017"
    MONGODB_DB: str = "agent_state"
    MONGODB_USERNAME: str = ""
    MONGODB_PASSWORD: str = ""
    MONGODB_AUTH_SOURCE: str = "admin"
    MONGODB_SESSIONS_COLLECTION: str = "sessions"
    MONGODB_TRACES_COLLECTION: str = "traces"
    MONGODB_STORE_BATCH_CONCURRENCY: int = 16

    # Event Merger Settings
    ENABLE_EVENT_MERGER: bool = True  # 是否启用事件合并
    EVENT_MERGE_INTERVAL: float = 300.0  # 合并间隔（秒，默认 1 分钟）
    EVENT_MERGE_BATCH_SIZE: int = 100
    EVENT_MERGE_CONCURRENCY: int = 3
    EVENT_MERGE_TIMEOUT_SECONDS: float = 120.0
    EVENT_MERGE_MAX_EVENTS_PER_TRACE: int = 5000

    # Memory Monitoring Settings
    MEMORY_MONITOR_ENABLED: bool = True
    MEMORY_MONITOR_INTERVAL_SECONDS: float = 60.0
    MEMORY_MONITOR_HISTORY_LIMIT: int = 60
    MEMORY_MONITOR_LEAK_THRESHOLD_MB: int = 128
    MEMORY_MONITOR_MIN_SAMPLES: int = 5
    MEMORY_MONITOR_ALERT_COOLDOWN_SECONDS: float = 600.0
    MEMORY_MONITOR_TRACEBACK_LIMIT: int = 8
    MEMORY_MONITOR_TOP_STATS_LIMIT: int = 8
    MEMORY_MONITOR_GC_OBJECT_LIMIT: int = 10
    MEMORY_MONITOR_HEAVY_DIAGNOSTICS: bool = False

    # Long-term Storage Settings
    ENABLE_POSTGRES_STORAGE: bool = False
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "postgres"
    POSTGRES_PASSWORD: str = "postgres"
    POSTGRES_DB: str = "langgraph"
    POSTGRES_POOL_MIN_SIZE: int = 2
    POSTGRES_POOL_MAX_SIZE: int = 10

    # Checkpoint Backend Settings
    CHECKPOINT_BACKEND: str = "mongodb"
    CHECKPOINT_PG_HOST: str = ""  # empty = fallback to POSTGRES_*
    CHECKPOINT_PG_PORT: int = 5432
    CHECKPOINT_PG_USER: str = ""
    CHECKPOINT_PG_PASSWORD: str = ""
    CHECKPOINT_PG_DB: str = ""
    CHECKPOINT_PG_POOL_MIN_SIZE: int = 2
    CHECKPOINT_PG_POOL_MAX_SIZE: int = 10

    # Checkpoint Retention Settings
    CHECKPOINT_CLEANUP_ENABLED: bool = False
    CHECKPOINT_CLEANUP_RETENTION_DAYS: int = 30
    CHECKPOINT_CLEANUP_INTERVAL_HOURS: int = 24
    CHECKPOINT_CLEANUP_BATCH_LIMIT: int = 200

    # Analytics Usage Settings
    ANALYTICS_USAGE_HOURLY_ENABLED: bool = False
    ANALYTICS_USAGE_HOURLY_FLUSH_SECONDS: int = 300
    ANALYTICS_USAGE_HOURLY_BACKFILL_INTERVAL_SECONDS: int = 3600
    ANALYTICS_USAGE_HOURLY_BACKFILL_BATCH_DAYS: int = 7

    # Sandbox Settings
    ENABLE_SANDBOX: bool = True
    SANDBOX_PLATFORM: str = "daytona"
    # Admin-authored capability boundary text injected into sandbox agent prompts.
    # Empty = no injection. Not a sandbox-manager rebuild key.
    SANDBOX_IMAGE_DESCRIPTION: str = ""
    DAYTONA_API_KEY: str = ""
    DAYTONA_SERVER_URL: str = ""
    DAYTONA_TIMEOUT: int = 180
    DAYTONA_IMAGE: str = ""
    SANDBOX_GREP_TIMEOUT: int = 30
    SANDBOX_MCP_REBUILD_CONCURRENCY: int = 4
    DAYTONA_AUTO_STOP_INTERVAL: int = 5
    DAYTONA_AUTO_ARCHIVE_INTERVAL: int = 5
    DAYTONA_AUTO_DELETE_INTERVAL: int = 1440

    # E2B Settings
    E2B_API_KEY: str = ""
    E2B_TEMPLATE: str = "base"
    E2B_TIMEOUT: int = 3600
    E2B_AUTO_PAUSE: bool = True
    E2B_AUTO_RESUME: bool = True

    # OpenSandbox Settings
    OPENSANDBOX_DOMAIN: str = ""
    OPENSANDBOX_API_KEY: str = ""
    OPENSANDBOX_IMAGE: str = "ubuntu"
    OPENSANDBOX_TIMEOUT: int = 3600
    OPENSANDBOX_WORK_DIR: str = "/root"
    # Server proxy: route execd/process requests through the OpenSandbox server
    # instead of connecting to sandbox container ports directly. Required when
    # the backend cannot reach the sandbox container network (k8s pod / host ->
    # server's Docker bridge). Defaults True for cross-network deployments.
    OPENSANDBOX_USE_SERVER_PROXY: bool = True
    # Revision marker for the dedicated multi-node config collection. Secrets are
    # never stored in this compatibility field.
    OPENSANDBOX_NODES: list[dict] = []

    # Skills Settings
    ENABLE_SKILLS: bool = True

    # Code Interpreter Settings
    ENABLE_CODE_INTERPRETER: bool = False

    # Tracing provider: none | langsmith | phoenix (mutually exclusive).
    TRACING_PROVIDER: str = "none"
    # LangSmith Tracing Settings (active when TRACING_PROVIDER=langsmith)
    LANGSMITH_API_KEY: Optional[str] = None
    LANGSMITH_PROJECT: str = "lamb-agent"
    LANGSMITH_API_URL: str = "https://api.smith.langchain.com"
    LANGSMITH_SAMPLE_RATE: float = 1.0
    # Phoenix Tracing Settings (active when TRACING_PROVIDER=phoenix)
    PHOENIX_COLLECTOR_ENDPOINT: str = "http://localhost:6006/v1/traces"
    PHOENIX_PROJECT_NAME: str = "lamb-agent"
    PHOENIX_API_KEY: Optional[str] = None

    # JWT Authentication Settings
    JWT_SECRET_KEY: str = Field(default_factory=lambda: secrets.token_urlsafe(32))
    JWT_ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_HOURS: int = 24
    REFRESH_TOKEN_EXPIRE_DAYS: int = 7
    LOGIN_IDLE_TIMEOUT_HOURS: float = 3.0

    # S3 Storage Settings
    S3_ENABLED: bool = False
    S3_PROVIDER: str = "aws"
    S3_ENDPOINT_URL: Optional[str] = None
    S3_ACCESS_KEY: str = ""
    S3_SECRET_KEY: str = ""
    S3_REGION: str = "us-east-1"
    S3_BUCKET_NAME: str = ""
    S3_CUSTOM_DOMAIN: Optional[str] = None
    S3_PATH_STYLE: bool = False
    S3_MAX_FILE_SIZE: int = 10 * 1024 * 1024
    S3_INTERNAL_UPLOAD_MAX_SIZE: int = 50 * 1024 * 1024
    S3_PUBLIC_BUCKET: bool = False
    S3_PRESIGNED_URL_EXPIRES: int = 7 * 24 * 3600

    # File Upload Settings
    LOCAL_STORAGE_PATH: str = "./uploads"
    ENABLE_LOCAL_FILESYSTEM_FALLBACK: bool = True
    FILE_UPLOAD_MAX_SIZE_IMAGE: int = 10
    FILE_UPLOAD_MAX_SIZE_VIDEO: int = 100
    FILE_UPLOAD_MAX_SIZE_AUDIO: int = 50
    FILE_UPLOAD_MAX_SIZE_DOCUMENT: int = 50
    FILE_UPLOAD_MAX_FILES: int = 10

    # User-owned storage quota settings.  The ledger remains authoritative even
    # when hard rejection is disabled during a rollout or rollback.
    USER_STORAGE_ENFORCEMENT_ENABLED: bool = True
    USER_STORAGE_DEFAULT_QUOTA_MB: int = 1024
    USER_STORAGE_WARNING_PERCENT: int = 80

    @field_validator("USER_STORAGE_DEFAULT_QUOTA_MB")
    @classmethod
    def validate_user_storage_quota(cls, value: int) -> int:
        if not 0 < int(value) <= (1 << 60) // (1024 * 1024):
            raise ValueError("USER_STORAGE_DEFAULT_QUOTA_MB is outside the safe byte range")
        return int(value)

    @field_validator("USER_STORAGE_WARNING_PERCENT")
    @classmethod
    def validate_user_storage_warning(cls, value: int) -> int:
        if not 1 <= int(value) <= 99:
            raise ValueError("USER_STORAGE_WARNING_PERCENT must be between 1 and 99")
        return int(value)

    # Frontend Settings
    FRONTEND_DEV_URL: str = ""
    DEFAULT_AGENT: str = "default"
    DEFAULT_MODEL_ID: str = ""
    WELCOME_SUGGESTIONS: list = Field(
        default_factory=lambda: [
            {"icon": "🐍", "text": "Create a Python hello world script"},
            {"icon": "📁", "text": "List files in the workspace directory"},
            {"icon": "📄", "text": "Read the README.md file"},
            {"icon": "🔧", "text": "Help me write a shell script"},
        ]
    )
    DEFAULT_USER_ROLE: str = "user"
    ENABLE_REGISTRATION: bool = True
    ADMIN_CONTACT_EMAIL: str = ""
    ADMIN_CONTACT_URL: str = ""

    # OAuth Settings
    OAUTH_GOOGLE_ENABLED: bool = False
    OAUTH_GOOGLE_CLIENT_ID: str = ""
    OAUTH_GOOGLE_CLIENT_SECRET: str = ""
    OAUTH_GITHUB_ENABLED: bool = False
    OAUTH_GITHUB_CLIENT_ID: str = ""
    OAUTH_GITHUB_CLIENT_SECRET: str = ""
    OAUTH_APPLE_ENABLED: bool = False
    OAUTH_APPLE_CLIENT_ID: str = ""
    OAUTH_APPLE_CLIENT_SECRET: str = ""
    OAUTH_APPLE_TEAM_ID: str = ""
    OAUTH_APPLE_KEY_ID: str = ""

    # OA SSO (enterprise portal)
    OA_SSO_ENABLED: bool = False
    OA_SSO_BASE_URL: str = "http://127.0.0.1"
    OA_SSO_PUBLIC_KEY: str = ""
    OA_SSO_CHANNEL_ID: str = "aimp"
    OA_SSO_EMAIL_DOMAIN: str = "ksrcb.com"
    OA_SSO_AUTO_PROVISION: bool = True
    OA_SSO_TIMEOUT_SECONDS: float = 60.0

    # Cloudflare Turnstile Settings
    TURNSTILE_ENABLED: bool = False
    TURNSTILE_SITE_KEY: str = ""
    TURNSTILE_SECRET_KEY: str = ""
    TURNSTILE_REQUIRE_ON_LOGIN: bool = False
    TURNSTILE_REQUIRE_ON_REGISTER: bool = True
    TURNSTILE_REQUIRE_ON_PASSWORD_CHANGE: bool = True

    # Email Settings (Resend)
    EMAIL_ENABLED: bool = False
    RESEND_ACCOUNTS: Any = Field(default_factory=list)
    PASSWORD_RESET_EXPIRE_HOURS: int = 24
    REQUIRE_EMAIL_VERIFICATION: bool = False

    # Memory Settings (Master Switch)
    ENABLE_MEMORY: bool = False

    # Scheduled tasks (user-defined timed agent runs)
    SCHEDULED_TASK_ENABLED: bool = True
    SCHEDULED_TASK_MAX_PER_USER: int = 20
    SCHEDULED_TASK_MIN_INTERVAL_HOURS: int = 1
    SCHEDULED_TASK_CHAT_TOOL_ENABLED: bool = True
    SCHEDULED_TASK_MAX_CONCURRENT_RUNS: int = 10
    SCHEDULED_TASK_QUEUE_TIMEOUT_MINUTES: int = 20

    # Personal knowledge base (admin-tunable)
    KB_ENABLED: bool = True
    KB_EMBEDDING_MODEL_ID: str = ""
    KB_RERANK_MODEL_ID: str = ""
    KB_WORKER_CONCURRENCY: int = 4
    KB_MAX_FILE_MB: int = 100
    KB_EMBEDDING_BATCH_SIZE: int = 32
    # Personal knowledge base (environment only)
    QDRANT_URL: str = ""
    QDRANT_API_KEY: str = ""
    QDRANT_LOCAL_PATH: str = "data/qdrant"
    KB_ARQ_QUEUE_NAME: str = "kunxiaozhi:kb"
    KB_JOB_TIMEOUT_SECONDS: int = 7200

    # Native Memory Settings (MongoDB-backed, zero external deps)
    NATIVE_MEMORY_EMBEDDING_MODEL_ID: str = ""
    NATIVE_MEMORY_STALENESS_DAYS: int = 30
    NATIVE_MEMORY_PRUNE_THRESHOLD: int = 90
    NATIVE_MEMORY_INDEX_ENABLED: bool = True
    NATIVE_MEMORY_INDEX_CACHE_TTL: int = 300
    NATIVE_MEMORY_MODEL_ID: str = ""
    NATIVE_MEMORY_COMPACTION_MODEL_ID: str = ""
    NATIVE_MEMORY_RERANK_MODEL_ID: str = ""
    NATIVE_MEMORY_MAX_TOKENS: int = 2000
    NATIVE_MEMORY_INLINE_CONTENT_MAX_CHARS: int = 1200
    NATIVE_MEMORY_IMPORT_TOTAL_CONTENT_MAX_CHARS: int = 2_000_000
    NATIVE_MEMORY_COMPACTION_CONTENT_MAX_CHARS: int = 4000
    NATIVE_MEMORY_CONSOLIDATION_INPUT_MAX_CHARS: int = 4000
    NATIVE_MEMORY_STORE_NAMESPACE: str = "memories"
    NATIVE_MEMORY_APPEND_MAX_DETAILS: int = 8
    NATIVE_MEMORY_RECALL_MIN_SCORE: float = 0.3
    NATIVE_MEMORY_HYDRATE_CONCURRENCY: int = 4
    NATIVE_MEMORY_CONSOLIDATION_ENRICH_CONCURRENCY: int = 4
    NATIVE_MEMORY_CONTENT_DELETE_CONCURRENCY: int = 4
    NATIVE_MEMORY_AUTO_COMPACT_ENABLED: bool = True
    NATIVE_MEMORY_AUTO_COMPACT_THRESHOLD: int = 40
    NATIVE_MEMORY_AUTO_COMPACT_INTERVAL_SECONDS: int = 43200
    NATIVE_MEMORY_AUTO_COMPACT_MIN_INTERVAL_SECONDS: int = 900
    NATIVE_MEMORY_AUTO_CAPTURE_INPUT_MAX_CHARS: int = 8000
    NATIVE_MEMORY_AUTO_CAPTURE_MAX_TASKS: int = 8

    # Audio transcription tool settings
    ENABLE_AUDIO_TRANSCRIPTION: bool = False
    AUDIO_TRANSCRIPTION_MODEL_ID: str = ""
    AUDIO_TRANSCRIPTION_MAX_DOWNLOAD_BYTES: int = 50 * 1024 * 1024

    # Vision assist settings (auxiliary vision model for non-vision main models)

    # Document parse tool settings (MinerU-backed document reader for non-sandbox agents)
    ENABLE_DOCUMENT_PARSE: bool = False
    MINERU_API_BASE_URL: str = "http://localhost:8000"
    MINERU_API_KEY: str = ""
    # hybrid "medium" force-disables figure analysis server-side, so the default
    # effort must be "high" for PDF figures/images to be described at all.
    MINERU_BACKEND: str = "hybrid-engine"
    MINERU_PARSE_EFFORT: str = "high"
    MINERU_IMAGE_ANALYSIS: bool = True
    DOCUMENT_PARSE_MAX_BYTES: int = 52428800
    DOCUMENT_PARSE_MAX_OUTPUT_CHARS: int = 50000

    # Image generation tool settings
    ENABLE_IMAGE_GENERATION: bool = False
    IMAGE_GENERATION_API_KEY: str = ""
    IMAGE_GENERATION_BASE_URL: str = "https://api.openai.com/v1"
    IMAGE_GENERATION_MODEL: str = "gpt-image-2"
    IMAGE_GENERATION_TIMEOUT: int = 120

    # Dify knowledge base retrieval tool settings
    DIFY_KB_ENABLED: bool = False
    DIFY_KB_BASE_URL: str = ""
    DIFY_KB_API_KEY: str = ""
    DIFY_KB_RERANK_MODEL_ID: str = ""
    DIFY_KB_SEARCH_METHOD: str = "hybrid_search"
    DIFY_KB_TOP_K: int = 10
    DIFY_KB_RERANK_TOP_K: int = 5
    DIFY_KB_SCORE_THRESHOLD: float = 0.4
    DIFY_KB_SEMANTIC_WEIGHT: float = 0.7
    DIFY_KB_DEFAULT_DATASET_IDS: list[str] = Field(default_factory=list)

    model_config = {
        "env_file": str(PROJECT_ROOT / ".env"),
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)

        # Generate random JWT_SECRET_KEY if not set or using placeholder
        if not self.JWT_SECRET_KEY or self.JWT_SECRET_KEY == "your-secret-key-change-in-production":
            self.JWT_SECRET_KEY = secrets.token_urlsafe(32)
            logger.warning(
                "JWT_SECRET_KEY not set or using placeholder value. "
                f"Generated random secret key: {self.JWT_SECRET_KEY[:8]}..."
            )
        # Expand short JWT_SECRET_KEY to meet minimum length requirement
        elif len(self.JWT_SECRET_KEY) < JWT_SECRET_KEY_MIN_LENGTH:
            original_key = self.JWT_SECRET_KEY
            self.JWT_SECRET_KEY = expand_jwt_secret_key(self.JWT_SECRET_KEY)
            logger.warning(
                f"JWT_SECRET_KEY too short ({len(original_key)} bytes). "
                f"Expanded to meet minimum {JWT_SECRET_KEY_MIN_LENGTH} bytes requirement. "
                f"Expanded key prefix: {self.JWT_SECRET_KEY[:8]}..."
            )

        # Generate random MCP_ENCRYPTION_SALT if not set
        if not self.MCP_ENCRYPTION_SALT:
            self.MCP_ENCRYPTION_SALT = secrets.token_urlsafe(16)
            logger.info("MCP_ENCRYPTION_SALT not set, generated random salt")
        # Expand short MCP_ENCRYPTION_SALT to meet minimum length requirement
        elif len(self.MCP_ENCRYPTION_SALT) < MCP_ENCRYPTION_SALT_MIN_LENGTH:
            original_salt = self.MCP_ENCRYPTION_SALT
            self.MCP_ENCRYPTION_SALT = expand_encryption_salt(self.MCP_ENCRYPTION_SALT)
            logger.warning(
                f"MCP_ENCRYPTION_SALT too short ({len(original_salt)} bytes). "
                f"Expanded to meet minimum {MCP_ENCRYPTION_SALT_MIN_LENGTH} bytes requirement. "
                f"Expanded salt prefix: {self.MCP_ENCRYPTION_SALT[:8]}..."
            )

        # Set version info from git (if not already set via env)
        if self.GIT_TAG is None:
            self.GIT_TAG = GIT_TAG
        if self.COMMIT_HASH is None:
            self.COMMIT_HASH = COMMIT_HASH
        if self.BUILD_TIME is None:
            self.BUILD_TIME = os.environ.get("BUILD_TIME")

        # Sync tracing env (LangSmith SDK + provider resolution). Full Phoenix
        # OTEL register happens in lifespan via init_tracing after DB settings load.
        from src.infra.tracing.provider import apply_tracing_env

        apply_tracing_env(self)

    @field_validator("DEBUG", mode="before")
    @classmethod
    def _normalize_debug_mode(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value

        normalized = value.strip().lower()
        if normalized in {"release", "prod", "production"}:
            return False
        if normalized in {"debug", "dev", "development"}:
            return True
        return value

    def get_s3_config(self) -> "S3Config":
        """Get S3 storage configuration."""
        from src.infra.storage.s3 import S3Config, S3Provider

        provider_map = {
            "aws": S3Provider.AWS,
            "aliyun": S3Provider.ALIYUN,
            "tencent": S3Provider.TENCENT,
            "minio": S3Provider.MINIO,
            "custom": S3Provider.CUSTOM,
            "local": S3Provider.LOCAL,
        }
        provider = provider_map.get(self.S3_PROVIDER.lower(), S3Provider.AWS)

        return S3Config(
            provider=provider,
            endpoint_url=self.S3_ENDPOINT_URL,
            access_key=self.S3_ACCESS_KEY,
            secret_key=self.S3_SECRET_KEY,
            region=self.S3_REGION,
            bucket_name=self.S3_BUCKET_NAME,
            custom_domain=self.S3_CUSTOM_DOMAIN,
            path_style=self.S3_PATH_STYLE,
            max_file_size=self.S3_MAX_FILE_SIZE,
            internal_max_upload_size=self.S3_INTERNAL_UPLOAD_MAX_SIZE,
            presigned_url_expires=self.S3_PRESIGNED_URL_EXPIRES,
            storage_path=self.LOCAL_STORAGE_PATH,
        )

    @property
    def postgres_url(self) -> str:
        """Construct PostgreSQL connection URL from components."""
        return f"postgresql://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def checkpoint_postgres_url(self) -> str:
        """Construct checkpoint PostgreSQL connection URL. Falls back to shared POSTGRES_* when CHECKPOINT_PG_HOST is empty."""
        host = self.CHECKPOINT_PG_HOST or self.POSTGRES_HOST
        port = self.CHECKPOINT_PG_PORT
        user = self.CHECKPOINT_PG_USER or self.POSTGRES_USER
        password = self.CHECKPOINT_PG_PASSWORD or self.POSTGRES_PASSWORD
        db = self.CHECKPOINT_PG_DB or self.POSTGRES_DB
        return f"postgresql://{user}:{password}@{host}:{port}/{db}"


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()


# Global settings instance
settings = get_settings()
