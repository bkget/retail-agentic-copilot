"use client";

import { KeyboardEvent, useEffect, useRef, useState } from "react";
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
const MAX_TEXTAREA_HEIGHT_PX = 200;

const STAGE_LABELS: Record<string, string> = {
  parsing_intent: "Reading your question…",
  thinking: "Thinking…",
  summarizing: "Summarizing results…",
};

// Generated fresh on every mount, deliberately not persisted - a reload starts a new
// conversation both visually and on the backend (see app.session.store.SessionStore):
// state/history from before a reload should not silently keep influencing follow-up
// resolution ("same as before") once the chat window that showed that context is
// gone. Only pinned questions (below) survive a reload.
function newSessionId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

interface PinnedQuestion {
  id: string;
  question: string;
}

const PINNED_STORAGE_KEY = "copilot-pinned-questions";
const MAX_PINNED = 10;

function loadPinned(): PinnedQuestion[] {
  try {
    const raw = window.localStorage.getItem(PINNED_STORAGE_KEY);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function savePinned(list: PinnedQuestion[]) {
  window.localStorage.setItem(PINNED_STORAGE_KEY, JSON.stringify(list));
}

export function ChatStream() {
  const [sessionId] = useState(newSessionId);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pinned, setPinned] = useState<PinnedQuestion[]>([]);
  const [question, setQuestion] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    setPinned(loadPinned());
  }, []);

  // Auto-grow the textarea with content, capped at MAX_TEXTAREA_HEIGHT_PX - beyond
  // that it scrolls internally instead of pushing the rest of the page around.
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  }, [question]);

  // Pinned by exact question text, not by turn id - two turns that happen to ask the
  // same thing share one pin, which is the more useful reading of "pin this question"
  // (a reusable piece of text) than "pin this specific message".
  function isPinned(text: string): boolean {
    return pinned.some((p) => p.question === text);
  }

  function togglePin(text: string) {
    setPinned((prev) => {
      const already = prev.some((p) => p.question === text);
      let next: PinnedQuestion[];
      if (already) {
        next = prev.filter((p) => p.question !== text);
      } else {
        const withNew = [...prev, { id: `${Date.now()}-${Math.random().toString(16).slice(2)}`, question: text }];
        // Cap at MAX_PINNED, evicting the oldest pin first (FIFO) rather than
        // silently refusing a new pin once the rail is full.
        next = withNew.length > MAX_PINNED ? withNew.slice(withNew.length - MAX_PINNED) : withNew;
      }
      savePinned(next);
      return next;
    });
  }

  // Copies the pinned text into the input box for the user to send (or edit first) -
  // deliberately does not auto-submit, so it extends the current conversation from
  // where the user left it rather than firing an unreviewed message on their behalf.
  function reuseQuestion(text: string) {
    setQuestion(text);
    textareaRef.current?.focus();
  }

  function updateTurn(id: string, patch: Partial<Turn>) {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...patch } : t)));
  }

  async function submitQuestion() {
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

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // Enter sends; Shift+Enter inserts a newline, like every chat UI this is modeled on.
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submitQuestion();
    }
  }

  return (
    <div>
      <aside className="pinned-sidebar" aria-label="Pinned questions">
        <p className="pinned-sidebar-title">Pinned questions</p>
        {pinned.length === 0 ? (
          <p className="pinned-empty">
            Pin a question from the conversation (hover it, click the pin) to reuse it here.
          </p>
        ) : (
          <ul className="pinned-list">
            {pinned.map((p) => (
              <li
                className="pinned-item"
                key={p.id}
                onClick={() => reuseQuestion(p.question)}
                title="Click to reuse in the input box"
              >
                <span className="pinned-item-text">{p.question}</span>
                <button
                  type="button"
                  className="pinned-item-unpin"
                  aria-label="Unpin question"
                  title="Unpin"
                  onClick={(e) => {
                    e.stopPropagation();
                    togglePin(p.question);
                  }}
                >
                  <CloseIcon />
                </button>
              </li>
            ))}
          </ul>
        )}
      </aside>

      <div className="message-list">
        {turns.map((t) => (
          <div className="turn" key={t.id}>
            <div className="user-row">
              <button
                type="button"
                className={`pin-button${isPinned(t.question) ? " is-pinned" : ""}`}
                onClick={() => togglePin(t.question)}
                aria-label={isPinned(t.question) ? "Unpin question" : "Pin question"}
                title={isPinned(t.question) ? "Unpin question" : "Pin question"}
              >
                <PinIcon filled={isPinned(t.question)} />
              </button>
              <div className="user-bubble">{t.question}</div>
            </div>

            <div className="response-card">
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
          </div>
        ))}
      </div>

      <form
        className="query-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submitQuestion();
        }}
      >
        <div className="query-input-wrap">
          <textarea
            ref={textareaRef}
            className="query-textarea"
            rows={1}
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder="Ask about sales, e.g. 'total revenue by division in 2020'"
            disabled={isStreaming}
          />
          <button
            className="query-submit"
            type="submit"
            disabled={isStreaming || !question.trim()}
            aria-label={isStreaming ? "Asking…" : "Ask"}
            title={isStreaming ? "Asking…" : "Ask"}
          >
            {isStreaming ? (
              <span className="query-submit-spinner" aria-hidden="true" />
            ) : (
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                <path
                  d="M12 19V5M12 5L6 11M12 5L18 11"
                  fill="none"
                  stroke="currentColor"
                  strokeWidth="2.2"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            )}
          </button>
        </div>
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

// A pushpin/thumbtack glyph (flat head, straight body, point protruding below) - the
// standard "pin this" icon used for pinning a message/item (Gmail, Notion, Slack), not
// the map-pin/location-marker teardrop shape this used before, which read as "place",
// not "pin".
function PinIcon({ filled }: { filled: boolean }) {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
      <path
        d="M16 12V4h1V2H7v2h1v8l-2 2v2h5.2v6h1.6v-6H18v-2l-2-2z"
        fill={filled ? "currentColor" : "none"}
        stroke="currentColor"
        strokeWidth={filled ? "0" : "1.3"}
        strokeLinejoin="round"
      />
    </svg>
  );
}

function CloseIcon() {
  return (
    <svg viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
      <path
        d="M6 6l12 12M18 6L6 18"
        stroke="currentColor"
        strokeWidth="2"
        strokeLinecap="round"
      />
    </svg>
  );
}
