import type { MetadataPayload } from "@/lib/types";

export function SqlAuditDrawer({
  sql,
  isTruncated,
  metadata,
}: {
  sql: string | null;
  isTruncated: boolean;
  metadata: MetadataPayload | null;
}) {
  if (!sql) return null;

  return (
    <details className="audit-drawer">
      <summary className="audit-summary">
        SQL &amp; trace{isTruncated ? " (results truncated at 500 rows)" : ""}
      </summary>
      <pre className="audit-sql">{sql}</pre>
      {metadata && (
        <div className="audit-metrics">
          <span>LLM {metadata.llm_ms}ms</span>
          <span>Guardrail {metadata.guardrail_ms}ms</span>
          <span>SQL {metadata.sql_ms}ms</span>
          <span>Total {metadata.total_ms}ms</span>
          <span>{metadata.row_count} rows</span>
          <span title={metadata.trace_id}>trace {metadata.trace_id.slice(0, 8)}</span>
        </div>
      )}
    </details>
  );
}
