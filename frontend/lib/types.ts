// Mirrors backend/app/sse/events.py and the response contract in db/README.md's linked
// project plan (section 6 of the original spec). Keep in sync by hand - there is no
// shared schema generation in this project's scope.

export interface VisualizationConfig {
  render_chart: boolean;
  chart_type: "bar" | "line" | "table" | null;
  title: string | null;
  x_axis_key: string | null; // bar/line only
  y_axis_key: string | null; // bar/line only
  columns: string[]; // table only: header order
  data: Record<string, string | number | null>[];
}

export type ResponseType = "conversational" | "clarification" | "query";

export interface QueryMetrics {
  llm_ms: number;
  guardrail_ms: number;
  sql_ms: number;
  total_ms: number;
}

export interface MetadataPayload extends QueryMetrics {
  // null for conversational/clarification turns - no query ran.
  row_count: number | null;
  is_truncated: boolean | null;
  trace_id: string;
  last_refreshed_at: string | null;
  response_type: ResponseType;
}

export interface DataSummary {
  row_count: number | null;
  is_truncated: boolean | null;
  formatting: { currency: string; unit: string; decimals: number };
}

export interface ConsolidatedResponse {
  schema_version: string;
  trace_id: string;
  status: "success" | "error";
  response_type: ResponseType;
  metrics: QueryMetrics;
  last_refreshed_at: string | null;
  data_summary: DataSummary;
  narrative: string;
  visualization: VisualizationConfig | null;
}

export type SseFrame =
  | { event: "status"; data: { stage: string } }
  | { event: "sql"; data: { sql_executed: string; is_truncated: boolean } }
  | { event: "narrative_delta"; data: { delta: string } }
  | { event: "visualization"; data: VisualizationConfig }
  | { event: "metadata"; data: MetadataPayload }
  | { event: "error"; data: { message: string } }
  | { event: "done"; data: ConsolidatedResponse };
