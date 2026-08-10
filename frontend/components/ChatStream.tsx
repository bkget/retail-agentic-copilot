"use client";

import { FormEvent, useState } from "react";
import { streamQuery } from "@/lib/sseClient";
import type { MetadataPayload, VisualizationConfig } from "@/lib/types";
import { ChartRenderer } from "./ChartRenderer";
import { SqlAuditDrawer } from "./SqlAuditDrawer";

interface Turn {
  id: string;
  question: string;
  stage: string | null;
  sql: string | null;
  isTruncated: boolean;
  narrative: string;
  visualization: VisualizationConfig | null;
  metadata: MetadataPayload | null;
  error: string | null;
  finished: boolean;
}

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

const STAGE_LABELS: Record<string, string> = {
  parsing_intent: "Reading your question…",
  thinking: "Thinking…",
  summarizing: "Summarizing results…",
};

function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

export function ChatStream() {
  const [sessionId] = useState(newSessionId);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [question, setQuestion] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);

  function updateTurn(id: string, patch: Partial<Turn>) {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }

  async function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const q = question.trim();
    if (!q || isStreaming) return;

    const id = `${Date.now()}`;
    setTurns((prev) => [
      ...prev,
      {
        id,
        question: q,
        stage: "parsing_intent",
        sql: null,
        isTruncated: false,
        narrative: "",
        visualization: null,
        metadata: null,
        error: null,
        finished: false,
      },
    ]);
    setQuestion("");
    setIsStreaming(true);

    try {
      for await (const frame of streamQuery(API_URL, q, sessionId)) {
        switch (frame.event) {
          case "status":
            updateTurn(id, { stage: frame.data.stage });
            break;
          case "sql":
            updateTurn(id, { sql: frame.data.sql_executed, isTruncated: frame.data.is_truncated });
            break;
          case "narrative_delta":
            setTurns((prev) =>
              prev.map((t) => (t.id === id ? { ...t, narrative: t.narrative + frame.data.delta } : t))
            );
            break;
          case "visualization":
            updateTurn(id, { visualization: frame.data });
            break;
          case "metadata":
            updateTurn(id, { metadata: frame.data, stage: null });
            break;
          case "error":
            updateTurn(id, { error: frame.data.message, stage: null, finished: true });
            break;
          case "done":
            updateTurn(id, { finished: true });
            break;
        }
      }
    } catch (err) {
      updateTurn(id, { error: err instanceof Error ? err.message : "Something went wrong.", stage: null });
    } finally {
      updateTurn(id, { finished: true });
      setIsStreaming(false);
    }
  }

  return (
    <div>
      <div className="message-list">
        {turns.map((t) => (
          <div className="message-card" key={t.id}>
            <p className="message-question">{t.question}</p>
            {t.stage && <div className="status-line">{STAGE_LABELS[t.stage] ?? t.stage}</div>}
            {t.error && <div className="error-text">{t.error}</div>}
            {t.narrative && (
              <p
                className="narrative-text"
                dangerouslySetInnerHTML={{ __html: boldMarkdown(t.narrative) }}
              />
            )}
            {t.visualization && <ChartRenderer config={t.visualization} />}
            <SqlAuditDrawer sql={t.sql} isTruncated={t.isTruncated} metadata={t.metadata} />
          </div>
        ))}
      </div>

      <form className="query-form" onSubmit={handleSubmit}>
        <input
          className="query-input"
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          placeholder="Ask about sales, e.g. 'total revenue by division in 2020'"
          disabled={isStreaming}
        />
        <button className="query-submit" type="submit" disabled={isStreaming || !question.trim()}>
          {isStreaming ? "Asking…" : "Ask"}
        </button>
      </form>
    </div>
  );
}

// Narrative text uses **bold** for emphasis (see backend/app/agent/narrative.py) - the
// only markdown construct it ever emits, so a full markdown renderer would be overkill.
function boldMarkdown(text: string): string {
  const escaped = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return escaped.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
}
