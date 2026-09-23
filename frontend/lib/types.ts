// Mirrors backend/app/sse/events.py and backend/app/main.py. Keep in sync by hand.

export type ChartType = "bar" | "line" | "multi_line" | "table";

export interface VisualizationConfig {
  render_chart: boolean;
  chart_type: ChartType | null;
  title: string | null;
  x_axis_key: string | null;
  y_axis_key: string | null;
  columns: string[]; // table header order (also the pivoted columns for multi_line)
  data: Record<string, string | number | null>[];
  series_keys?: string[]; // multi_line only
}

export type ResponseType = "conversational" | "clarification" | "query";

export interface QueryMetrics {
  llm_ms: number;
  guardrail_ms: number;
  sql_ms: number;
  total_ms: number;
}

export interface MetadataPayload extends QueryMetrics {
  row_count: number | null;
  is_truncated: boolean | null;
  trace_id: string;
  last_refreshed_at: string | null;
  response_type: ResponseType;
  interpreted_as?: string | null;
}

export type StepStatus = "running" | "done" | "warning" | "error";

export interface ThinkingStep {
  id: string;
  label: string;
  detail: string | null;
  status: StepStatus;
  elapsed_ms?: number | null;
}

export interface ConsolidatedResponse {
  schema_version: string;
  trace_id: string;
  status: "success" | "error";
  response_type: ResponseType;
  metrics: QueryMetrics;
  last_refreshed_at: string | null;
  narrative: string;
  visualization: VisualizationConfig | null;
  suggestions: string[];
  interpreted_as: string | null;
}

export type SseFrame =
  | { event: "status"; data: { stage: string } }
  | { event: "step"; data: ThinkingStep }
  | { event: "sql"; data: { sql_executed: string; is_truncated: boolean } }
  | { event: "narrative_delta"; data: { delta: string } }
  | { event: "visualization"; data: VisualizationConfig }
  | { event: "suggestions"; data: { items: string[]; response_type: ResponseType } }
  | { event: "metadata"; data: MetadataPayload }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: ConsolidatedResponse };

export interface HistoryTurn {
  question: string;
  sql: string | null;
  row_count: number | null;
  narrative: string | null;
  response_type: ResponseType;
  visualization: VisualizationConfig | null;
  suggestions: string[];
}

export interface Capabilities {
  year_min: number | null;
  year_max: number | null;
  examples: string[];
  llm_provider: string;
  nlu_fallback: string;
}
