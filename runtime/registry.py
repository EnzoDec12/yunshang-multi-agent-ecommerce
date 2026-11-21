"""工具契约定义（ToolDefinition）、全局注册表（ToolRegistry）与最小权限快照（ToolSnapshot）。

核心安全红线：
1. 工具命名强制遵循 `<domain>__<action>` 域前缀规范（如 `oms__search_orders`、`pim__hybrid_search`）；
2. `description` 必须显式包含负向使用边界说明，降低大模型幻觉误调；
3. 严禁在 `input_schema`（含嵌套子模型与别名）中暴露 `user_id`、`tenant_id`、`role`、`is_admin`、`approved`、`risk_level` 等可信上下文字段；
4. `input_schema`（含嵌套子模型）必须显式声明 `extra="forbid"`，阻断模型在调用参数中夹带未声明伪造字段；
5. 严禁向 Agent 直接暴露全量 `ToolRegistry`，仅允许通过 `build_snapshot(allowed_tools)` 导出裁剪后的只读 `ToolSnapshot`。
"""

from __future__ import annotations

import copy
import inspect
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict

from schemas.context import ExecutionContext, RoleType
from schemas.contracts import ErrorPayload, RiskLevel

DOMAIN_QUALIFIED_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*__[a-z][a-z0-9_]*$")

FORBIDDEN_INPUT_FIELDS: frozenset[str] = frozenset(
    {
        "user_id",
        "tenant_id",
        "role",
        "is_admin",
        "approved",
        "risk_level",
        "permissions",
        "hitl_approvals",
        "approved_tool_call_ids",
        "trace_id",
    }
)

NEGATIVE_BOUNDARY_MARKERS: tuple[str, ...] = (
    "禁止",
    "不得",
    "不用于",
    "不支持",
    "不可",
    "切勿",
    "勿用于",
    "不包括",
    "仅限",
    "非",
    "do not",
    "don't",
    "never",
    "not for",
    "must not",
    "cannot",
    "only for",
)

ToolHandler = Callable[[Any, ExecutionContext], Awaitable[BaseModel | dict[str, Any]]]
ToolDependencyCheck = Callable[[], Awaitable[bool] | bool]


class ToolRegistrationError(ValueError):
    """工具契约定义或注册违反架构红线时抛出的异常。"""


class StrictToolInput(BaseModel):
    """推荐工具入参基类：默认开启 `extra='forbid'` 与 `frozen=True`。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class StrictToolOutput(BaseModel):
    """推荐工具出参基类：默认开启 `extra='ignore'` 自动剔除后端多余内部字段。"""

    model_config = ConfigDict(extra="ignore", frozen=True)


def _is_async_callable(func: Any) -> bool:
    if inspect.iscoroutinefunction(func):
        return True
    if callable(func) and inspect.iscoroutinefunction(getattr(func, "__call__", None)):
        return True
    return False


def _extract_alias_strings(alias_obj: Any) -> list[str]:
    if alias_obj is None:
        return []
    if isinstance(alias_obj, str):
        return [alias_obj]
    if isinstance(alias_obj, AliasPath):
        return [str(part) for part in alias_obj.convert_to_aliases() if isinstance(part, str)]
    if isinstance(alias_obj, AliasChoices):
        extracted: list[str] = []
        for choice in alias_obj.choices:
            extracted.extend(_extract_alias_strings(choice))
        return extracted
    return []


def _iter_nested_models(annotation: Any) -> list[type[BaseModel]]:
    models: list[type[BaseModel]] = []
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        models.append(annotation)
        return models

    origin = get_origin(annotation)
    if origin is None:
        return models

    # 覆盖 Annotated / Union / list / dict / tuple / set 等泛型参数
    for arg in get_args(annotation):
        if arg is type(None):
            continue
        models.extend(_iter_nested_models(arg))
    return models


def _validate_input_schema_security(
    schema_cls: Any,
    *,
    tool_name: str,
    visited: set[type[BaseModel]] | None = None,
) -> None:
    if not (isinstance(schema_cls, type) and issubclass(schema_cls, BaseModel)):
        raise ToolRegistrationError(
            f"工具 '{tool_name}' 的 input_schema 必须是 Pydantic v2 BaseModel 子类"
        )

    if visited is None:
        visited = set()
    if schema_cls in visited:
        return
    visited.add(schema_cls)

    extra_policy = schema_cls.model_config.get("extra")
    if extra_policy != "forbid":
        raise ToolRegistrationError(
            f"工具 '{tool_name}' 的输入模型 '{schema_cls.__name__}' 必须配置 "
            f"model_config = ConfigDict(extra='forbid')（当前为 {extra_policy!r}），"
            "以拦截大模型在参数中夹带伪造字段"
        )

    for field_name, field_info in schema_cls.model_fields.items():
        candidate_names = [
            field_name,
            *_extract_alias_strings(field_info.alias),
            *_extract_alias_strings(field_info.validation_alias),
        ]
        for candidate in candidate_names:
            normalized = candidate.strip().lower()
            if normalized in FORBIDDEN_INPUT_FIELDS:
                raise ToolRegistrationError(
                    f"工具 '{tool_name}' 的输入模型 '{schema_cls.__name__}' 包含禁止暴露给模型的"
                    f"可信上下文字段 '{candidate}'；身份与审批态必须仅由 ExecutionContext 注入"
                )

        for nested_model in _iter_nested_models(field_info.annotation):
            _validate_input_schema_security(
                nested_model, tool_name=tool_name, visited=visited
            )


def _validate_description_boundary(description: str, *, tool_name: str) -> str:
    cleaned = description.strip()
    if len(cleaned) < 10:
        raise ToolRegistrationError(
            f"工具 '{tool_name}' 的 description 过短，必须清晰说明用途与负向边界"
        )
    lowered = cleaned.lower()
    if not any(marker in lowered for marker in NEGATIVE_BOUNDARY_MARKERS):
        raise ToolRegistrationError(
            f"工具 '{tool_name}' 的 description 必须包含显式负向边界说明"
            "（例如：'不用于...' / '禁止...' / 'Do not...'）"
        )
    return cleaned


@dataclass(frozen=True, kw_only=True)
class ToolDefinition:
    """单个工具的完整强类型契约定义（不可变）。"""

    name: str
    description: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    error_schema: type[BaseModel] = ErrorPayload
    permission: str
    risk: RiskLevel
    handler: ToolHandler
    allowed_roles: frozenset[RoleType] | set[RoleType] | None = None
    dependency_check: ToolDependencyCheck | None = None

    def __post_init__(self) -> None:
        if not DOMAIN_QUALIFIED_NAME_RE.match(self.name):
            raise ToolRegistrationError(
                f"工具名 '{self.name}' 不符合 '<domain>__<action>' 域前缀规范"
                "（示例：'oms__search_orders'、'pim__hybrid_search'）"
            )

        cleaned_desc = _validate_description_boundary(
            self.description, tool_name=self.name
        )
        object.__setattr__(self, "description", cleaned_desc)

        _validate_input_schema_security(self.input_schema, tool_name=self.name)

        if not (
            isinstance(self.output_schema, type)
            and issubclass(self.output_schema, BaseModel)
        ):
            raise ToolRegistrationError(
                f"工具 '{self.name}' 的 output_schema 必须是 Pydantic v2 BaseModel 子类"
            )

        if not (
            isinstance(self.error_schema, type)
            and issubclass(self.error_schema, BaseModel)
        ):
            raise ToolRegistrationError(
                f"工具 '{self.name}' 的 error_schema 必须是 Pydantic v2 BaseModel 子类"
            )

        cleaned_perm = self.permission.strip()
        if not cleaned_perm:
            raise ToolRegistrationError(
                f"工具 '{self.name}' 必须声明非空 RBAC permission 标识"
            )
        object.__setattr__(self, "permission", cleaned_perm)

        if self.risk not in ("low", "medium", "high"):
            raise ToolRegistrationError(
                f"工具 '{self.name}' 的 risk 必须为 'low' / 'medium' / 'high' 之一"
            )

        if not _is_async_callable(self.handler):
            raise ToolRegistrationError(
                f"工具 '{self.name}' 的 handler 必须是 async 异步可调用对象"
            )

        if self.allowed_roles is not None:
            frozen_roles = frozenset(self.allowed_roles)
            if not frozen_roles or not frozen_roles.issubset({"customer", "merchant"}):
                raise ToolRegistrationError(
                    f"工具 '{self.name}' 的 allowed_roles 仅允许包含 'customer' 或 'merchant'"
                )
            object.__setattr__(self, "allowed_roles", frozen_roles)

    @property
    def domain(self) -> str:
        """提取工具所属业务域前缀（如 `oms`、`pim`、`analytics`）。"""
        return self.name.split("__", 1)[0]

    def to_llm_schema(self) -> dict[str, Any]:
        """仅导出供大模型推理使用的裁剪后工具 JSON Schema（不含权限、内部句柄与风控实现）。"""
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": copy.deepcopy(self.input_schema.model_json_schema()),
        }


class ToolSnapshot:
    """裁剪后的最小权限工具快照（只读，不持有全量 ToolRegistry 引用）。"""

    __slots__ = ("_allowed_names", "_bindings", "_llm_schemas")

    def __init__(self, tools: Sequence[ToolDefinition]) -> None:
        ordered_map: dict[str, ToolDefinition] = {}
        schemas: list[dict[str, Any]] = []
        for tool in tools:
            if tool.name not in ordered_map:
                ordered_map[tool.name] = tool
                schemas.append(tool.to_llm_schema())

        self._allowed_names: tuple[str, ...] = tuple(ordered_map.keys())
        self._bindings: Mapping[str, ToolDefinition] = MappingProxyType(ordered_map)
        self._llm_schemas: tuple[dict[str, Any], ...] = tuple(schemas)

    @property
    def allowed_tools(self) -> tuple[str, ...]:
        """当前快照允许调用的工具限定名只读元组。"""
        return self._allowed_names

    def contains(self, tool_name: str) -> bool:
        """检查工具是否在当前快照白名单内。"""
        return tool_name in self._bindings

    def __contains__(self, tool_name: object) -> bool:
        return isinstance(tool_name, str) and tool_name in self._bindings

    def __len__(self) -> int:
        return len(self._bindings)

    def to_llm_schemas(self) -> list[dict[str, Any]]:
        """导出给 Agent / 大模型的裁剪后工具 JSON Schema 列表（每次返回深拷贝防篡改）。"""
        return copy.deepcopy(list(self._llm_schemas))

    def resolve(self, tool_name: str) -> ToolDefinition | None:
        """供 `ToolRuntime` 在当前快照边界内解析工具定义句柄。"""
        return self._bindings.get(tool_name)


class ToolRegistry:
    """全局工具注册中心（仅在启动装配阶段使用，严禁直接暴露给任何 Agent）。"""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> ToolDefinition:
        """注册单个工具契约并执行命名与 Schema 安全校验。"""
        if not isinstance(tool, ToolDefinition):
            raise ToolRegistrationError("仅允许注册 ToolDefinition 实例")
        if tool.name in self._tools:
            raise ToolRegistrationError(f"工具 '{tool.name}' 已存在，禁止重复注册覆盖")
        self._tools[tool.name] = tool
        return tool

    def has_tool(self, tool_name: str) -> bool:
        """检查指定工具名是否已完成注册。"""
        return tool_name in self._tools

    @property
    def registered_count(self) -> int:
        """已注册工具总数。"""
        return len(self._tools)

    def list_tool_names(
        self,
        *,
        role: RoleType | None = None,
        domains: Sequence[str] | None = None,
    ) -> tuple[str, ...]:
        """按可选角色与业务域过滤返回已注册工具限定名只读元组。"""
        domain_filter = frozenset(domains) if domains is not None else None
        matched: list[str] = []
        for name, tool in self._tools.items():
            if domain_filter is not None and tool.domain not in domain_filter:
                continue
            if (
                role is not None
                and tool.allowed_roles is not None
                and role not in tool.allowed_roles
            ):
                continue
            matched.append(name)
        return tuple(matched)

    def build_snapshot(self, allowed_tools: Sequence[str]) -> ToolSnapshot:
        """按白名单裁剪构建专属 `ToolSnapshot`，不导出任何未授权工具。"""
        selected: list[ToolDefinition] = []
        seen: set[str] = set()
        for name in allowed_tools:
            if name not in self._tools:
                raise ToolRegistrationError(
                    f"无法构建 ToolSnapshot：请求的工具 '{name}' 未在 ToolRegistry 中注册"
                )
            if name not in seen:
                seen.add(name)
                selected.append(self._tools[name])
        return ToolSnapshot(selected)

    def build_role_snapshot(
        self,
        role: RoleType,
        *,
        domains: Sequence[str] | None = None,
        permissions: frozenset[str] | set[str] | None = None,
    ) -> ToolSnapshot:
        """按 C/B 双域角色（及可选域/RBAC 权限集合）裁剪生成最小权限 `ToolSnapshot`。"""
        if role not in ("customer", "merchant"):
            raise ToolRegistrationError(
                f"构建角色快照失败：不支持的角色类型 {role!r}"
            )
        domain_filter = frozenset(domains) if domains is not None else None
        selected: list[ToolDefinition] = []
        for tool in self._tools.values():
            if domain_filter is not None and tool.domain not in domain_filter:
                continue
            if tool.allowed_roles is not None and role not in tool.allowed_roles:
                continue
            if permissions is not None and tool.permission not in permissions:
                continue
            selected.append(tool)
        return ToolSnapshot(selected)
