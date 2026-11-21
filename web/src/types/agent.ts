export type RoleType = "customer" | "merchant";
export type PerspectiveId = "customer_01" | "merchant_01";
export type RiskLevel = "low" | "medium" | "high" | "unknown";
export type ToolStatus = "running" | "success" | "error" | "hitl_required";

export interface AccountProfile {
  account_id: string;
  display_name: string;
  tenant_id: string;
  user_id: string;
  role: RoleType;
  permissions: string[];
  badge: string;
  description: string;
  primary_demo?: boolean;
}

export interface LoginResponse {
  access_token: string;
  token_type: string;
  expires_in: number;
  account: AccountProfile;
}

export interface ErrorPayload {
  error_type: "transport_error" | "permission_error" | "validation_error" | "business_error";
  code: string;
  message: string;
  retryable: boolean;
  details?: Record<string, unknown> | null;
}

export interface SkuComparisonEntry {
  sku_id: string;
  name: string;
  category: string;
  retail_price: number;
  available_stock: number;
  sizes?: string;
  color?: string;
  fabric?: string;
  specs: Record<string, string>;
  selling_points: string[];
  rating: number;
}

export interface CompareSkusData {
  compared_count: number;
  min_price: number;
  max_price: number;
  entries: SkuComparisonEntry[];
  comparison_summary: string;
}

export interface CheckStockData {
  sku_id: string;
  sku_name: string;
  available_stock: number;
  required_qty: number;
  warehouse: string;
  stock_status: string;
}

export interface OrderRecord {
  order_id: string;
  status: "pending" | "paid" | "shipped" | "completed" | "refunded" | string;
  created_at: string;
  sku_id: string;
  sku_name: string;
  quantity: number;
  total_amount: number;
  buyer_name: string;
  buyer_phone: string;
  buyer_email?: string;
  shipping_address: string;
  tracking_no?: string | null;
  refund_id?: string | null;
}

export interface LogisticsCheckpoint {
  timestamp: string;
  location: string;
  status: string;
}

export interface LogisticsPackageData {
  tracking_no: string;
  order_id: string;
  carrier: string;
  delivery_status: string;
  estimated_delivery: string;
  receiver_name: string;
  receiver_phone: string;
  receiver_address: string;
  checkpoints: LogisticsCheckpoint[];
}

export interface CreateRefundData {
  refund_id: string;
  order_id: string;
  refund_amount: number;
  order_status: string;
  reason: string;
  operator_user_id: string;
}

export interface UpdateAddressData {
  order_id: string;
  status: string;
  receiver_name: string;
  receiver_phone: string;
  shipping_address: string;
  updated_by: string;
}

export interface AnomalousMetric {
  metric_name: string;
  current_value: number;
  benchmark_value: number;
  delta_pct: number;
  summary: string;
}

export interface ReturnReasonStat {
  reason: string;
  share: number;
  order_count: number;
}

export interface PriceBandGap {
  min_price: number;
  max_price: number;
  demand_index: number;
  gap_description: string;
}

export interface DiagnosisArtifact {
  problem_category: string;
  anomalous_metrics: AnomalousMetric[];
  top_return_reasons: ReturnReasonStat[];
  price_band_gaps: PriceBandGap[];
}

export interface CandidateSKU {
  sku_id: string;
  sku_name: string;
  target_price: number;
  estimated_unit_cost: number;
}

export interface SelectionProposal {
  candidate_skus: CandidateSKU[];
  estimated_gross_margin: number;
  core_selling_points: string[];
  risk_warnings: string[];
}

export interface CommittedProposalData {
  proposal_id: string;
  sku_name: string;
  category: string;
  target_retail_price: number;
  estimated_unit_cost: number;
  estimated_gross_margin: number;
  initial_order_qty: number;
  core_selling_points: string[];
  knowledge_base_synced: boolean;
  synced_resource_uri: string;
  flywheel_kb_verified?: boolean;
  status: string;
}

export interface RefundConfirmationDetails {
  order_id: string;
  refund_amount: number;
  reason: string;
  sku_id?: string | null;
  sku_name?: string | null;
  quantity?: number | null;
  total_amount?: number | null;
  order_status?: string | null;
  created_at?: string | null;
  buyer_name?: string | null;
  buyer_phone?: string | null;
  shipping_address?: string | null;
}

export interface ThinkingEvent {
  thread_id: string;
  trace_id: string;
  node: string;
  agent: string;
  domain: RoleType;
  message: string;
  diagnosis_artifact?: DiagnosisArtifact | null;
}

export interface ToolStartEvent {
  thread_id: string;
  trace_id: string;
  tool_call_id: string;
  tool_name: string;
  domain: string;
  agent: string;
  risk: RiskLevel;
  arguments: Record<string, unknown>;
}

export interface ToolResultEvent {
  thread_id: string;
  trace_id: string;
  tool_call_id: string;
  tool_name: string;
  domain: string;
  agent: string;
  risk: RiskLevel;
  status: "success" | "error" | "hitl_required";
  duration_ms: number;
  data: Record<string, unknown> | null;
  error: ErrorPayload | null;
  hitl: Record<string, unknown> | null;
}

export interface HITLInterruptEvent {
  thread_id: string;
  trace_id: string;
  domain: RoleType;
  gate_type: "high_risk_tool" | "address_confirmation";
  agent: string;
  tool_call_id: string;
  tool_name: string;
  risk: RiskLevel;
  permission: string;
  reason: string;
  arguments: Record<string, unknown>;
  refund_details?: RefundConfirmationDetails | null;
  selection_proposal?: SelectionProposal | null;
  diagnosis_artifact?: DiagnosisArtifact | null;
}

export interface TurnArtifacts {
  sku_comparisons: CompareSkusData[];
  search_results: Record<string, unknown>[];
  stock_checks: CheckStockData[];
  orders: OrderRecord[];
  logistics: LogisticsPackageData[];
  refund_results: CreateRefundData[];
  address_updates: UpdateAddressData[];
  diagnosis_artifact: DiagnosisArtifact | null;
  selection_proposal: SelectionProposal | null;
  committed_proposal: CommittedProposalData | null;
  approval_status: "approved" | "rejected" | null;
  circuit_breaker_triggered: boolean;
  circuit_breaker_reason: string | null;
}

export interface FinalAnswerEvent {
  thread_id: string;
  trace_id: string;
  domain: RoleType;
  answer: string;
  final_response: string;
  artifacts: TurnArtifacts;
}

export interface ToolStepRecord {
  tool_call_id: string;
  tool_name: string;
  domain: string;
  agent: string;
  risk: RiskLevel;
  status: ToolStatus;
  arguments: Record<string, unknown>;
  duration_ms?: number;
  data?: Record<string, unknown> | null;
  error?: ErrorPayload | null;
}

export interface ConversationTurn {
  id: string;
  userMessage: string;
  createdAt: string;
  threadId?: string;
  traceId?: string;
  isStreaming: boolean;
  activeThinking?: ThinkingEvent | null;
  thinkingHistory: ThinkingEvent[];
  toolOrder: string[];
  toolsById: Record<string, ToolStepRecord>;
  artifacts: TurnArtifacts;
  pendingHitl?: HITLInterruptEvent | null;
  hitlDecisionNote?: {
    decision: "approve" | "reject";
    comment: string;
  } | null;
  finalAnswer?: string | null;
  streamError?: string | null;
}

export interface AuditLogEntry {
  timestamp: string;
  trace_id: string;
  tool_call_id: string;
  tenant_id: string;
  user_id: string;
  role: RoleType;
  tool_name: string;
  domain: string;
  risk: RiskLevel;
  status: "success" | "error" | "hitl_required";
  error_type?: string | null;
  error_code?: string | null;
  guard_reason?: string | null;
  intercepted: boolean;
  duration_ms: number;
}

export interface AuditLogsResponse {
  total: number;
  viewer: {
    tenant_id: string;
    user_id: string;
    role: RoleType;
  };
  logs: AuditLogEntry[];
}
