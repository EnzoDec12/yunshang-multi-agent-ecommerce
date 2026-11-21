"""统一配置中心（`config/settings.py`）、LLM 路由工厂解耦与仓库配置卫生单元测试。"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from agents.llm import LLMMessage, LiteLLMModel, build_configured_llm, create_llm_client
from agents.mock_llm import DeterministicEcommerceMockLLM
from config.settings import AppSettings, get_settings, reload_settings
from gateway.auth import get_jwt_secret, issue_access_token, verify_jwt_and_build_context, PRESET_ACCOUNTS
from gateway.main import ManagedMCPCluster, create_gateway_app
from runtime.mcp_adapter import MCPClientManager, build_mcp_server_configs

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent

_MANAGED_ENV_KEYS: tuple[str, ...] = (
    "APP_ENV",
    "DATABASE_URL",
    "JWT_SECRET",
    "GATEWAY_JWT_SECRET",
    "JWT_TTL_SECONDS",
    "GATEWAY_JWT_TTL_SECONDS",
    "GATEWAY_CORS_ORIGINS",
    "LLM_MODE",
    "ECOMMERCE_LLM_PROVIDER",
    "LLM_MODEL",
    "LITELLM_MODEL",
    "LLM_API_KEY",
    "LITELLM_API_KEY",
    "OPENAI_API_KEY",
    "LLM_API_BASE",
    "LITELLM_API_BASE",
    "LLM_TIMEOUT_SECONDS",
    "LLM_MAX_RETRIES",
    "LLM_FALLBACK_MODELS",
    "LLM_FALLBACK_TO_MOCK",
    "REQUIRE_ADDRESS_HITL",
    "ECOMMERCE_REQUIRE_ADDRESS_HITL",
    "MCP_SPAWN_LOCAL_SUBPROCESS",
    "MCP_PIM_URL",
    "MCP_OMS_URL",
    "MCP_LOGISTICS_URL",
    "MCP_BI_URL",
    "MCP_MARKET_ERP_URL",
    "MCP_EXTERNAL_ENDPOINTS_JSON",
    "MCP_CLIENT_TIMEOUT_SECONDS",
    "MCP_CLIENT_CONNECT_TIMEOUT_SECONDS",
    "TOOL_RUNTIME_TIMEOUT_SECONDS",
)


def _clear_managed_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _MANAGED_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_default_settings_and_env_example_completeness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证无外部环境变量时的默认配置及 `.env.example` 字段齐备性。"""
    _clear_managed_env(monkeypatch)
    cfg = AppSettings(_env_file=None)

    assert cfg.APP_ENV == "dev"
    assert cfg.LLM_MODE == "mock"
    assert cfg.LLM_MODEL == "gpt-4o-mini"
    assert cfg.LLM_API_KEY is None
    assert cfg.DATABASE_URL is None
    assert cfg.is_postgres_configured is False
    assert cfg.MCP_SPAWN_LOCAL_SUBPROCESS is True
    assert len(cfg.JWT_SECRET) >= 16

    assert cfg.mcp_domain_urls() == {
        "pim": "http://127.0.0.1:18101/mcp",
        "oms": "http://127.0.0.1:18102/mcp",
        "logistics": "http://127.0.0.1:18103/mcp",
        "bi": "http://127.0.0.1:18104/mcp",
        "market_erp": "http://127.0.0.1:18105/mcp",
    }

    env_example = WORKSPACE_ROOT / ".env.example"
    assert env_example.is_file(), "根目录缺少 .env.example"
    env_example_text = env_example.read_text(encoding="utf-8")
    required_keys = (
        "APP_ENV=",
        "JWT_SECRET=",
        "LLM_MODE=",
        "LLM_MODEL=",
        "LLM_API_KEY=",
        "DATABASE_URL=",
        "MCP_SPAWN_LOCAL_SUBPROCESS=",
        "MCP_PIM_URL=",
        "MCP_OMS_URL=",
        "MCP_LOGISTICS_URL=",
        "MCP_BI_URL=",
        "MCP_MARKET_ERP_URL=",
    )
    for key_prefix in required_keys:
        assert key_prefix in env_example_text, f".env.example 缺少配置项 {key_prefix}"

    gitignore = WORKSPACE_ROOT / ".gitignore"
    assert gitignore.is_file(), "根目录缺少 .gitignore"
    gitignore_text = gitignore.read_text(encoding="utf-8")
    for ignored_pattern in (".demo-runtime/", ".env", ".venv/", "__pycache__/", "web/node_modules/", "web/.next/"):
        assert ignored_pattern in gitignore_text

    assert not (WORKSPACE_ROOT / "docker" / "init.sql").is_dir(), "误建的 docker/init.sql/ 目录应当已清理"


def test_env_overrides_dotenv_loading_and_spawn_subprocess_smart_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """验证环境变量覆盖、`.env` 文件读取以及 `MCP_SPAWN_LOCAL_SUBPROCESS` 按环境智能推导。"""
    _clear_managed_env(monkeypatch)

    # 1) 生产环境未显式指定 MCP_SPAWN_LOCAL_SUBPROCESS 时自动默认为 False
    monkeypatch.setenv("APP_ENV", "PROD")
    monkeypatch.setenv("JWT_SECRET", "prod-override-super-secret-hs256-key-2026")
    monkeypatch.setenv("LLM_MODE", "litellm")
    monkeypatch.setenv("LLM_MODEL", "openai/gpt-4.1-mini")
    monkeypatch.setenv("LLM_API_KEY", "sk-test-override-key")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://prod_user:pwd@db:5432/yunshang_prod")
    monkeypatch.setenv("MCP_PIM_URL", "http://pim-svc:8101/mcp")
    monkeypatch.setenv("MCP_OMS_URL", "http://oms-svc:8102/mcp")
    monkeypatch.setenv("MCP_LOGISTICS_URL", "http://logistics-svc:8103/mcp")
    monkeypatch.setenv("MCP_BI_URL", "http://bi-svc:8104/mcp")
    monkeypatch.setenv("MCP_MARKET_ERP_URL", "http://erp-svc:8105/mcp")
    monkeypatch.setenv("GATEWAY_CORS_ORIGINS", "https://shop.yunshang.cn, https://admin.yunshang.cn")
    monkeypatch.setenv("LLM_FALLBACK_MODELS", "anthropic/claude-3-5-haiku-latest,deepseek/deepseek-chat")

    prod_cfg = reload_settings()
    assert prod_cfg.APP_ENV == "prod"
    assert prod_cfg.MCP_SPAWN_LOCAL_SUBPROCESS is False
    assert prod_cfg.JWT_SECRET == "prod-override-super-secret-hs256-key-2026"
    assert prod_cfg.LLM_MODE == "litellm"
    assert prod_cfg.LLM_MODEL == "openai/gpt-4.1-mini"
    assert prod_cfg.LLM_API_KEY == "sk-test-override-key"
    assert prod_cfg.DATABASE_URL == "postgresql+psycopg://prod_user:pwd@db:5432/yunshang_prod"
    assert prod_cfg.GATEWAY_CORS_ORIGINS == [
        "https://shop.yunshang.cn",
        "https://admin.yunshang.cn",
    ]
    assert prod_cfg.LLM_FALLBACK_MODELS == [
        "anthropic/claude-3-5-haiku-latest",
        "deepseek/deepseek-chat",
    ]
    assert prod_cfg.mcp_domain_urls() == {
        "pim": "http://pim-svc:8101/mcp",
        "oms": "http://oms-svc:8102/mcp",
        "logistics": "http://logistics-svc:8103/mcp",
        "bi": "http://bi-svc:8104/mcp",
        "market_erp": "http://erp-svc:8105/mcp",
    }

    # 2) 显式环境变量开关优先于 APP_ENV 默认推导（dev 下显式关闭、prod 下显式开启）
    monkeypatch.setenv("APP_ENV", "dev")
    monkeypatch.setenv("MCP_SPAWN_LOCAL_SUBPROCESS", "false")
    assert get_settings().MCP_SPAWN_LOCAL_SUBPROCESS is False

    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("MCP_SPAWN_LOCAL_SUBPROCESS", "true")
    assert get_settings().MCP_SPAWN_LOCAL_SUBPROCESS is True

    # 3) 验证从自定义 .env 文件加载配置，且网关 JWT 与 MCP 适配器同步生效
    _clear_managed_env(monkeypatch)
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "\n".join(
            [
                "APP_ENV=prod",
                "JWT_SECRET=dotenv-loaded-hs256-signing-secret-key-9999",
                "MCP_CLIENT_TIMEOUT_SECONDS=12.5",
                "MCP_PIM_URL=http://dotenv-pim:9101/mcp",
            ]
        ),
        encoding="utf-8",
    )
    dotenv_cfg = AppSettings(_env_file=dotenv_file)
    assert dotenv_cfg.APP_ENV == "prod"
    assert dotenv_cfg.MCP_SPAWN_LOCAL_SUBPROCESS is False
    assert get_jwt_secret(dotenv_cfg) == "dotenv-loaded-hs256-signing-secret-key-9999"

    tok, _ = issue_access_token(PRESET_ACCOUNTS["customer_01"], app_settings=dotenv_cfg)
    ctx, _ = verify_jwt_and_build_context(tok, app_settings=dotenv_cfg)
    assert ctx.user_id == "buyer_1001"

    mcp_configs = build_mcp_server_configs(dotenv_cfg)
    pim_cfg = next(c for c in mcp_configs if c.domain == "pim")
    assert pim_cfg.url == "http://dotenv-pim:9101/mcp"
    assert pim_cfg.timeout_seconds == 12.5

    manager = MCPClientManager.from_settings(dotenv_cfg)
    assert set(manager.domains) == {"pim", "oms", "logistics", "bi", "market_erp"}

    # 4) 非法枚举或过短密钥严格触发 ValidationError
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, APP_ENV="staging")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        AppSettings(_env_file=None, JWT_SECRET="too-short")


async def test_llm_router_factory_decoupling_and_litellm_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """验证业务 Mock 已从 `agents/llm.py` 抽离，且路由工厂支持超时与多级 Fallback 降级。"""
    _clear_managed_env(monkeypatch)

    # 1) AST 校验：4 个核心模块不再散落 os.environ 硬编码读取，且 agents/llm.py 不定义业务 Mock 类
    target_files = (
        WORKSPACE_ROOT / "gateway" / "auth.py",
        WORKSPACE_ROOT / "gateway" / "main.py",
        WORKSPACE_ROOT / "agents" / "llm.py",
        WORKSPACE_ROOT / "runtime" / "mcp_adapter.py",
    )
    for py_path in target_files:
        source = py_path.read_text(encoding="utf-8")
        assert "os.environ" not in source, f"{py_path.name} 仍残留未收敛的 os.environ 读取"

    llm_tree = ast.parse((WORKSPACE_ROOT / "agents" / "llm.py").read_text(encoding="utf-8"))
    defined_classes = {n.name for n in ast.walk(llm_tree) if isinstance(n, ast.ClassDef)}
    assert "DeterministicEcommerceMockLLM" not in defined_classes

    # 2) mock 模式下工厂返回确定性推理器
    mock_settings = AppSettings(_env_file=None, LLM_MODE="mock", REQUIRE_ADDRESS_HITL=True)
    mock_client, mock_label = build_configured_llm(mock_settings)
    assert isinstance(mock_client, DeterministicEcommerceMockLLM)
    assert mock_label == "mock:DeterministicEcommerceMockLLM"

    # 3) litellm 模式下返回带超时、重试与 Fallback 链的 LiteLLMModel，验证主模型超时自动切换备选模型
    litellm_settings = AppSettings(
        _env_file=None,
        LLM_MODE="litellm",
        LLM_MODEL="openai/gpt-4o",
        LLM_API_KEY="sk-primary",
        LLM_TIMEOUT_SECONDS=12.0,
        LLM_MAX_RETRIES=1,
        LLM_FALLBACK_MODELS=["anthropic/claude-3-5-haiku-latest"],
    )
    prod_client = create_llm_client(litellm_settings)
    assert isinstance(prod_client, LiteLLMModel)
    assert prod_client.timeout == 12.0
    assert prod_client.max_retries == 1
    assert prod_client.fallback_models == ("anthropic/claude-3-5-haiku-latest",)

    import litellm

    attempted_models: list[str] = []

    async def fake_acompletion_with_primary_timeout(**kwargs: Any) -> Any:
        model_name = str(kwargs["model"])
        attempted_models.append(model_name)
        assert kwargs["timeout"] == 12.0
        assert kwargs["num_retries"] == 1
        if model_name == "openai/gpt-4o":
            raise TimeoutError("Primary LLM timed out")
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="备选模型成功完成降级响应",
                        tool_calls=None,
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=8, total_tokens=18),
        )

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion_with_primary_timeout)
    fallback_resp = await prod_client.ainvoke(
        [LLMMessage(role="user", content="测试主模型超时降级")],
        agent_name="C-Supervisor",
    )
    assert attempted_models == ["openai/gpt-4o", "anthropic/claude-3-5-haiku-latest"]
    assert fallback_resp.content == "备选模型成功完成降级响应"

    # 4) 验证开启 LLM_FALLBACK_TO_MOCK 时，远程模型全灭可兜底回退至本地确定性推理器
    async def fake_all_remote_down(**kwargs: Any) -> Any:
        raise ConnectionError("All remote LLM providers unreachable")

    monkeypatch.setattr(litellm, "acompletion", fake_all_remote_down)
    resilient_client = create_llm_client(
        AppSettings(
            _env_file=None,
            LLM_MODE="litellm",
            LLM_MODEL="openai/gpt-4o",
            LLM_FALLBACK_TO_MOCK=True,
        )
    )
    degraded_resp = await resilient_client.ainvoke(
        [LLMMessage(role="user", content="你好")],
        agent_name="C-Supervisor",
    )
    assert "云裳女装旗舰店" in degraded_resp.content


async def test_gateway_external_mcp_urls_when_spawn_subprocess_disabled() -> None:
    """验证 `MCP_SPAWN_LOCAL_SUBPROCESS=False`（生产/Docker 模式）时网关不拉起本地子进程，直接连接 5 域 URL。"""
    external_cluster = ManagedMCPCluster()
    urls = await external_cluster.start()
    try:
        prod_settings = AppSettings(
            _env_file=None,
            APP_ENV="prod",
            JWT_SECRET="prod-docker-gateway-hs256-signing-secret-2026",
            MCP_PIM_URL=urls["pim"],
            MCP_OMS_URL=urls["oms"],
            MCP_LOGISTICS_URL=urls["logistics"],
            MCP_BI_URL=urls["bi"],
            MCP_MARKET_ERP_URL=urls["market_erp"],
        )
        assert prod_settings.MCP_SPAWN_LOCAL_SUBPROCESS is False

        gw_app = create_gateway_app(app_settings=prod_settings)
        async with gw_app.router.lifespan_context(gw_app):
            container = gw_app.state.container
            assert container.mcp_cluster is None
            assert container.mcp_endpoints == urls
            async with AsyncClient(
                transport=ASGITransport(app=gw_app),
                base_url="http://testserver",
            ) as client:
                resp = await client.get("/health")
                assert resp.status_code == 200
                body = resp.json()
                assert body["status"] == "ok"
                assert body["registered_tools_count"] == 13
                assert set(body["mcp_domains"]) == {
                    "pim",
                    "oms",
                    "logistics",
                    "bi",
                    "market_erp",
                }
    finally:
        await external_cluster.stop()

