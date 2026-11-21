"""统一配置中心（基于 `pydantic-settings` 集中管理网关、编排层、LLM 与 5 域 MCP 环境变量）。"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

DEFAULT_JWT_SECRET = "oppo-ecommerce-multi-agent-gateway-hs256-secret-2026-prod-grade"
DEFAULT_CORS_ORIGINS: tuple[str, ...] = (
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://localhost:3001",
    "http://127.0.0.1:3001",
)


def _parse_string_list(raw: Any, *, default: tuple[str, ...] = ()) -> list[str]:
    """兼容解析逗号分隔字符串、JSON 数组字符串或 Python 序列。"""
    if raw is None:
        return list(default)
    if isinstance(raw, (list, tuple, set, frozenset)):
        return [str(item).strip() for item in raw if str(item).strip()]
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return list(default)
        if text.startswith("["):
            parsed = json.loads(text)
            if not isinstance(parsed, list):
                raise ValueError("JSON 列表配置必须为字符串数组")
            return [str(item).strip() for item in parsed if str(item).strip()]
        return [part.strip() for part in text.split(",") if part.strip()]
    raise ValueError(f"无法将 {type(raw).__name__} 解析为字符串列表")


class AppSettings(BaseSettings):
    """全栈环境变量与运行配置契约。"""

    model_config = SettingsConfigDict(
        env_file=DEFAULT_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
        case_sensitive=False,
    )

    # 1. 运行环境、日志与基础设施连接
    APP_ENV: Literal["dev", "prod"] = Field(
        default="dev",
        description="运行环境标识：dev（本地开发/离线演示）或 prod（生产/Docker 容器化部署）",
    )
    LOG_LEVEL: str = Field(
        default="INFO",
        min_length=1,
        description="全局结构化 JSON 日志级别（DEBUG/INFO/WARNING/ERROR）",
    )
    DATABASE_URL: str | None = Field(
        default=None,
        description="PostgreSQL 持久化连接串；未配置时自动回退至内存态 MemorySaver 与 InMemoryAuditStore",
    )

    # 可选可观测平台（Langfuse，未配置密钥时零报错静默跳过）
    LANGFUSE_PUBLIC_KEY: str | None = Field(
        default=None,
        description="Langfuse Public Key（可选）",
    )
    LANGFUSE_SECRET_KEY: str | None = Field(
        default=None,
        description="Langfuse Secret Key（可选）",
    )
    LANGFUSE_HOST: str = Field(
        default="https://cloud.langfuse.com",
        validation_alias=AliasChoices("LANGFUSE_HOST", "LANGFUSE_BASE_URL"),
        description="Langfuse 服务端地址",
    )

    # 2. API Gateway JWT 鉴权与跨域安全配置
    JWT_SECRET: str = Field(
        default=DEFAULT_JWT_SECRET,
        min_length=16,
        validation_alias=AliasChoices("JWT_SECRET", "GATEWAY_JWT_SECRET"),
        description="API Gateway HMAC-SHA256 JWT 签名密钥（至少 16 字符）",
    )
    JWT_ALGORITHM: str = Field(
        default="HS256",
        min_length=1,
        description="JWT 签名算法",
    )
    JWT_ISSUER: str = Field(
        default="oppo-ecommerce-agent-gateway",
        min_length=1,
        description="JWT 签发方标识（iss）",
    )
    JWT_TTL_SECONDS: int = Field(
        default=8 * 3600,
        ge=60,
        validation_alias=AliasChoices("JWT_TTL_SECONDS", "GATEWAY_JWT_TTL_SECONDS"),
        description="JWT 访问令牌有效期（秒，最低 60 秒）",
    )
    GATEWAY_CORS_ORIGINS: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: list(DEFAULT_CORS_ORIGINS),
        description="允许跨域访问网关的前端 Origin 列表（逗号分隔或 JSON 数组）",
    )

    # 3. 大模型路由、超时重试与降级配置
    LLM_MODE: Literal["mock", "litellm"] = Field(
        default="mock",
        validation_alias=AliasChoices("LLM_MODE", "ECOMMERCE_LLM_PROVIDER"),
        description="大模型推理后端模式：mock（确定性离线推理）或 litellm（真实大模型调用）",
    )
    LLM_MODEL: str = Field(
        default="gpt-4o-mini",
        min_length=1,
        validation_alias=AliasChoices("LLM_MODEL", "LITELLM_MODEL"),
        description="LiteLLM 主模型标识（如 openai/gpt-4o-mini、deepseek/deepseek-chat）",
    )
    LLM_API_KEY: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_KEY", "LITELLM_API_KEY", "OPENAI_API_KEY"),
        description="大模型服务 API Key（mock 模式下可留空）",
    )
    LLM_API_BASE: str | None = Field(
        default=None,
        validation_alias=AliasChoices("LLM_API_BASE", "LITELLM_API_BASE"),
        description="可选的兼容 OpenAI 协议网关 Base URL",
    )
    LLM_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        gt=0.0,
        description="LiteLLM 单次异步请求超时时间（秒）",
    )
    LLM_MAX_RETRIES: int = Field(
        default=2,
        ge=0,
        le=10,
        description="LiteLLM 瞬态错误最大自动重试次数",
    )
    LLM_FALLBACK_MODELS: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="主模型超时或故障时按序尝试的备选 LiteLLM 模型列表（逗号分隔）",
    )
    LLM_FALLBACK_TO_MOCK: bool = Field(
        default=False,
        description="当全部远程 LiteLLM 模型均不可用时，是否自动降级回退至本地确定性推理器",
    )
    REQUIRE_ADDRESS_HITL: bool = Field(
        default=False,
        validation_alias=AliasChoices("REQUIRE_ADDRESS_HITL", "ECOMMERCE_REQUIRE_ADDRESS_HITL"),
        description="未发货订单修改收货地址是否强制开启人工二次确认挂起",
    )

    # 4. 五域独立 MCP Server 进程托管与端点连接配置
    MCP_SPAWN_LOCAL_SUBPROCESS: bool = Field(
        default=True,
        description="是否在网关进程内自动拉起本地 5 域 MCP 子进程（dev 默认 True，prod/Docker 默认 False）",
    )
    MCP_PIM_URL: str = Field(
        default="http://127.0.0.1:18101/mcp",
        pattern=r"^https?://.+",
        description="PIM 商品与库存域 Streamable HTTP MCP 端点",
    )
    MCP_OMS_URL: str = Field(
        default="http://127.0.0.1:18102/mcp",
        pattern=r"^https?://.+",
        description="OMS 订单与售后域 Streamable HTTP MCP 端点",
    )
    MCP_LOGISTICS_URL: str = Field(
        default="http://127.0.0.1:18103/mcp",
        pattern=r"^https?://.+",
        description="Logistics 物流履约域 Streamable HTTP MCP 端点",
    )
    MCP_BI_URL: str = Field(
        default="http://127.0.0.1:18104/mcp",
        pattern=r"^https?://.+",
        description="BI 经营数仓域 Streamable HTTP MCP 端点",
    )
    MCP_MARKET_ERP_URL: str = Field(
        default="http://127.0.0.1:18105/mcp",
        pattern=r"^https?://.+",
        description="Market & ERP 选品与供应链域 Streamable HTTP MCP 端点",
    )
    MCP_EXTERNAL_ENDPOINTS_JSON: str | None = Field(
        default=None,
        description="可选的外部 MCP 端点整包覆盖 JSON 字典（domain -> url）",
    )
    MCP_CLIENT_TIMEOUT_SECONDS: float = Field(
        default=8.0,
        gt=0.0,
        description="MCP Client 单次 RPC 读写超时时间（秒）",
    )
    MCP_CLIENT_CONNECT_TIMEOUT_SECONDS: float = Field(
        default=3.0,
        gt=0.0,
        description="MCP Client 建立底层 HTTP 连接超时时间（秒）",
    )
    TOOL_RUNTIME_TIMEOUT_SECONDS: float = Field(
        default=30.0,
        gt=0.0,
        description="ToolRuntime 单次工具执行总超时护栏（秒）",
    )

    @model_validator(mode="before")
    @classmethod
    def _apply_env_dependent_defaults(cls, data: Any) -> Any:
        if not isinstance(data, Mapping):
            return data
        mutable = dict(data)

        raw_app_env: Any = "dev"
        has_explicit_spawn = False
        spawn_key_found: str | None = None

        for key, val in list(mutable.items()):
            upper_key = str(key).upper()
            if upper_key == "APP_ENV" and val is not None and str(val).strip() != "":
                raw_app_env = val
            elif upper_key == "MCP_SPAWN_LOCAL_SUBPROCESS":
                spawn_key_found = str(key)
                if val is not None and str(val).strip() != "":
                    has_explicit_spawn = True

        if not has_explicit_spawn:
            if spawn_key_found is not None:
                mutable.pop(spawn_key_found, None)
            normalized_env = str(raw_app_env).strip().lower()
            mutable["MCP_SPAWN_LOCAL_SUBPROCESS"] = normalized_env != "prod"

        return mutable

    @field_validator("APP_ENV", mode="before")
    @classmethod
    def _normalize_app_env(cls, value: Any) -> Any:
        if isinstance(value, str):
            return value.strip().lower()
        return value

    @field_validator("LLM_MODE", mode="before")
    @classmethod
    def _normalize_llm_mode(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip().lower()
            return cleaned or "mock"
        return value

    @field_validator("LLM_MODEL", mode="before")
    @classmethod
    def _normalize_llm_model(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned or "gpt-4o-mini"
        return value

    @field_validator(
        "DATABASE_URL",
        "LLM_API_KEY",
        "LLM_API_BASE",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "MCP_EXTERNAL_ENDPOINTS_JSON",
        mode="before",
    )
    @classmethod
    def _empty_str_to_none(cls, value: Any) -> Any:
        if isinstance(value, str):
            cleaned = value.strip()
            return cleaned if cleaned else None
        return value

    @field_validator("GATEWAY_CORS_ORIGINS", mode="before")
    @classmethod
    def _validate_cors_origins(cls, value: Any) -> list[str]:
        return _parse_string_list(value, default=DEFAULT_CORS_ORIGINS)

    @field_validator("LLM_FALLBACK_MODELS", mode="before")
    @classmethod
    def _validate_fallback_models(cls, value: Any) -> list[str]:
        return _parse_string_list(value, default=())

    @property
    def is_postgres_configured(self) -> bool:
        """判断是否配置了有效的 PostgreSQL `DATABASE_URL`。"""
        if not self.DATABASE_URL:
            return False
        lowered = self.DATABASE_URL.strip().lower()
        return lowered.startswith(
            ("postgresql://", "postgres://", "postgresql+psycopg://", "postgresql+asyncpg://")
        )

    @property
    def is_langfuse_configured(self) -> bool:
        """判断是否同时配置了 Langfuse 公私钥。"""
        return bool(self.LANGFUSE_PUBLIC_KEY and self.LANGFUSE_SECRET_KEY)

    def mcp_domain_urls(self) -> dict[str, str]:
        """返回标准 5 域 MCP Streamable HTTP 端点映射表。"""
        if self.MCP_EXTERNAL_ENDPOINTS_JSON:
            parsed = json.loads(self.MCP_EXTERNAL_ENDPOINTS_JSON)
            if not isinstance(parsed, dict):
                raise ValueError("MCP_EXTERNAL_ENDPOINTS_JSON 必须为 domain -> url 的 JSON 对象")
            return {str(k): str(v) for k, v in parsed.items()}

        return {
            "pim": self.MCP_PIM_URL,
            "oms": self.MCP_OMS_URL,
            "logistics": self.MCP_LOGISTICS_URL,
            "bi": self.MCP_BI_URL,
            "market_erp": self.MCP_MARKET_ERP_URL,
        }


def get_settings() -> AppSettings:
    """读取当前环境变量与 `.env` 生成最新不可篡改配置快照。"""
    return AppSettings()


settings: AppSettings = get_settings()


def reload_settings() -> AppSettings:
    """重新加载全局 `settings` 快照并返回新实例。"""
    global settings
    settings = get_settings()
    return settings
