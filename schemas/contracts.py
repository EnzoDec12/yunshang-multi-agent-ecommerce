"""工具调用协议、标准化错误载荷、HITL 挂起契约与多 Agent 强类型交接契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from schemas.context import RoleType

ErrorType = Literal[
    "transport_error",
    "permission_error",
    "validation_error",
    "business_error",
]
RiskLevel = Literal["low", "medium", "high"]
ToolExecutionStatus = Literal["success", "error", "hitl_required"]


class ErrorPayload(BaseModel):
    """标准化四分类工具错误载荷。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    error_type: ErrorType = Field(
        ...,
        description="严格区分 transport_error / permission_error / validation_error / business_error",
    )
    code: str = Field(..., min_length=1, description="机器可读稳定错误码")
    message: str = Field(..., min_length=1, description="脱敏后的结构化错误描述")
    retryable: bool = Field(default=False, description="调用方是否可安全重试")
    details: dict[str, Any] | None = Field(
        default=None, description="可选结构化错误上下文（如字段校验明细）"
    )

    @property
    def category(self) -> ErrorType:
        """与 `error_type` 等价的只读错误分类属性，兼容编排层 `error.category` 读取。"""
        return self.error_type


class HITLRequired(BaseModel):
    """高风险工具未获受信任审批时的标准挂起信号载荷。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str = Field(..., min_length=1, description="触发挂起的原始 tool_call_id")
    tool_name: str = Field(..., min_length=1, description="触发挂起的高风险工具名")
    risk: Literal["high"] = Field(default="high", description="风险等级固定为 high")
    permission: str = Field(..., min_length=1, description="该操作关联的权限标识")
    reason: str = Field(..., min_length=1, description="要求人工介入审批的原因说明")
    sanitized_arguments: dict[str, Any] = Field(
        default_factory=dict, description="供人工审核展示的脱敏后入参快照"
    )
    arguments_digest: str = Field(
        ..., min_length=1, description="规范化入参 SHA-256 摘要，用于审批绑定防篡改"
    )
    trace_id: str = Field(..., min_length=1, description="关联链路 trace_id")
    tenant_id: str = Field(..., min_length=1, description="关联租户 ID")
    user_id: str = Field(..., min_length=1, description="发起请求的用户 ID")


class HITLInterruptError(Exception):
    """可选异常封装：供需要通过异常栈中断编排节点的宿主层捕获 `HITLRequired` 信号。"""

    def __init__(self, signal: HITLRequired) -> None:
        super().__init__(signal.reason)
        self.signal = signal


class ToolBusinessError(Exception):
    """工具 Handler 显式抛出的业务规则错误（映射为 `business_error`）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "BUSINESS_RULE_VIOLATION",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class ToolTransportError(Exception):
    """工具底层通信或依赖不可用异常（映射为 `transport_error`）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "UPSTREAM_TRANSPORT_ERROR",
        retryable: bool = True,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = details


class ToolPermissionError(PermissionError):
    """工具或远端 MCP 服务显式拒绝访问异常（映射为 `permission_error`）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "HANDLER_PERMISSION_DENIED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class ToolValidationError(ValueError):
    """工具或远端 MCP 服务参数语义校验失败异常（映射为 `validation_error`）。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "HANDLER_VALIDATION_FAILED",
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


class ToolCall(BaseModel):
    """大模型发起的单次工具调用请求契约。"""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    tool_call_id: str = Field(..., min_length=1, description="单次工具调用唯一对齐 ID")
    name: str = Field(
        ...,
        min_length=1,
        validation_alias=AliasChoices("name", "tool_name"),
        description="带域前缀的工具限定名（如 oms__search_orders）",
    )
    arguments: dict[str, Any] = Field(
        default_factory=dict, description="模型生成的业务查询/操作参数字典"
    )

    @property
    def tool_name(self) -> str:
        """与 `name` 等价的只读属性，兼容不同宿主层命名习惯。"""
        return self.name


class ToolResultMessage(BaseModel):
    """Tool Runtime 标准回执消息，强制携带原始 `tool_call_id` 防止并发串台。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_call_id: str = Field(..., min_length=1, description="严格透传原始 ToolCall.tool_call_id")
    tool_name: str | None = Field(default=None, description="工具限定名")
    status: ToolExecutionStatus = Field(
        ..., description="执行结果状态：success / error / hitl_required"
    )
    data: dict[str, Any] | None = Field(
        default=None, description="经 Output Schema 裁剪与隐私脱敏后的业务数据"
    )
    error: ErrorPayload | None = Field(
        default=None, description="当 status == 'error' 时携带的四分类错误载荷"
    )
    hitl: HITLRequired | None = Field(
        default=None, description="当 status == 'hitl_required' 时携带的挂起信号载荷"
    )

    @model_validator(mode="after")
    def _validate_status_payload_invariants(self) -> ToolResultMessage:
        if self.status == "success":
            if self.data is None:
                raise ValueError("status='success' 时 data 不得为 None")
            if self.error is not None or self.hitl is not None:
                raise ValueError("status='success' 时 error 与 hitl 必须为 None")
        elif self.status == "error":
            if self.error is None:
                raise ValueError("status='error' 时必须携带 ErrorPayload")
            if self.data is not None or self.hitl is not None:
                raise ValueError("status='error' 时 data 与 hitl 必须为 None")
        elif self.status == "hitl_required":
            if self.hitl is None:
                raise ValueError("status='hitl_required' 时必须携带 HITLRequired 载荷")
            if self.data is not None or self.error is not None:
                raise ValueError("status='hitl_required' 时 data 与 error 必须为 None")
        return self


class AuditLogEntry(BaseModel):
    """Tool Runtime 后置结构化审计日志记录。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime = Field(..., description="UTC 记录时间戳")
    trace_id: str = Field(..., min_length=1)
    tool_call_id: str = Field(..., min_length=1)
    tenant_id: str = Field(..., min_length=1)
    user_id: str = Field(..., min_length=1)
    role: RoleType
    tool_name: str = Field(..., min_length=1)
    risk: RiskLevel | Literal["unknown"]
    status: ToolExecutionStatus
    error_type: ErrorType | None = None
    duration_ms: float = Field(..., ge=0.0, description="单次工具闭环执行耗时（毫秒）")


# ============================================================================
# 多 Agent 强类型交接契约（B 端经营诊断 Agent -> 选品企划 Agent）
# 严禁跨 Agent 透传冗长原始对话历史，一律通过以下冻结结构化 Artifact 交接。
# ============================================================================


class AnomalousMetric(BaseModel):
    """经营诊断识别出的单项异常指标。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    metric_name: str = Field(..., min_length=1, description="指标名称（如 return_rate、conversion_rate）")
    current_value: float = Field(..., description="当前周期实测值")
    benchmark_value: float = Field(..., description="行业/历史基线对比值")
    delta_pct: float = Field(..., description="相对基线偏离百分比（如 +135.0 表示高于基线 135%）")
    summary: str = Field(..., min_length=1, description="指标异常简要归因")


class ReturnReasonStat(BaseModel):
    """退货原因结构化占比项。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    reason: str = Field(..., min_length=1, description="标准化退货原因标签")
    share: float = Field(..., gt=0.0, le=1.0, description="占该品类总退货单比例 (0, 1]")
    order_count: int = Field(default=0, ge=0, description="对应退货单量")


class PriceBandGap(BaseModel):
    """存在供给空白或高潜需求的价格带缺口。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    min_price: float = Field(..., ge=0.0, description="价格带下界（元）")
    max_price: float = Field(..., gt=0.0, description="价格带上界（元），必须大于 min_price")
    demand_index: float = Field(
        default=1.0, ge=0.0, description="需求热度或搜索转化缺口指数"
    )
    gap_description: str = Field(..., min_length=1, description="价格带缺口机会说明")

    @model_validator(mode="after")
    def _check_price_bounds(self) -> PriceBandGap:
        if self.max_price <= self.min_price:
            raise ValueError("价格带缺口 max_price 必须严格大于 min_price")
        return self


class DiagnosisArtifact(BaseModel):
    """B 端经营数据分析 Agent 输出给选品 Agent 的强类型诊断交接物。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    problem_category: str = Field(..., min_length=1, description="存在经营异常的核心问题品类")
    anomalous_metrics: list[AnomalousMetric] = Field(
        ..., min_length=1, description="核心异常经营指标列表（至少 1 项）"
    )
    top_return_reasons: list[ReturnReasonStat] = Field(
        ..., min_length=1, max_length=3, description="Top3 退货原因统计（1~3 项）"
    )
    price_band_gaps: list[PriceBandGap] = Field(
        ..., min_length=1, description="识别出的高潜价格带缺口列表"
    )

    @field_validator("problem_category")
    @classmethod
    def _strip_category(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("problem_category 不得为空白字符串")
        return stripped


class CandidateSKU(BaseModel):
    """选品方案中的单个候选商品 SKU。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    sku_id: str = Field(..., min_length=1, description="候选商品标准 SKU 编码")
    sku_name: str = Field(..., min_length=1, description="商品名称")
    target_price: float = Field(..., gt=0.0, description="建议上架零售价（元）")
    estimated_unit_cost: float = Field(default=0.0, ge=0.0, description="预估单件供货/生产成本（元）")

    @model_validator(mode="after")
    def _validate_cost_vs_price(self) -> CandidateSKU:
        if self.estimated_unit_cost > self.target_price:
            raise ValueError("候选 SKU 预估单件成本不得高于建议零售价")
        return self


class SelectionProposal(BaseModel):
    """B 端选品企划 Agent 基于 `DiagnosisArtifact` 生成的强类型选品提案交接物。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_skus: list[CandidateSKU] = Field(
        ..., min_length=1, description="推荐引入的候选 SKU 列表"
    )
    estimated_gross_margin: float = Field(
        ..., ge=0.0, le=1.0, description="组合预估毛利率，范围 [0.0, 1.0]"
    )
    core_selling_points: list[str] = Field(
        ..., min_length=1, description="针对历史退货痛点与价格带缺口提炼的核心卖点"
    )
    risk_warnings: list[str] = Field(
        ..., min_length=1, description="供应链、库存周转或合规风险提示"
    )

    @field_validator("core_selling_points", "risk_warnings")
    @classmethod
    def _validate_non_empty_items(cls, items: list[str]) -> list[str]:
        cleaned = [item.strip() for item in items if item and item.strip()]
        if len(cleaned) != len(items):
            raise ValueError("列表元素不得包含空白字符串")
        return cleaned
