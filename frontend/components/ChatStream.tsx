"use client";

import { KeyboardEvent, useCallback, useEffect, useRef, useState } from "react";
import { clearSession, fetchCapabilities, fetchHistory, streamQuery } from "@/lib/sseClient";
import type { MetadataPayload, ResponseType, ThinkingStep, VisualizationConfig } from "@/lib/types";
import { ChartRenderer } from "./ChartRenderer";
import { Markdown } from "./Markdown";
import { SqlAuditDrawer } from "./SqlAuditDrawer";
import { ThinkingPanel, TypingDots } from "./ThinkingPanel";

interface Turn {
  id: string;
  question: string;
  steps: ThinkingStep[];
  startedAt: number | null;
  thinkingMs: number | null; // time until the first answer token
  sql: string | null;
  isTruncated: boolean;
  narrative: string;
  visualization: VisualizationConfig | null;
  metadata: MetadataPayload | null;
  responseType: ResponseType | null;
  suggestions: string[];
  error: string | null;
  stopped: boolean;
  finished: boolean;
}

// Empty (default) = same origin; /api/* is proxied to the backend by next.config.mjs.
const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "";
const MAX_TEXTAREA_HEIGHT_PX = 200;
const SESSION_KEY = "copilot-session-id";
const PINNED_STORAGE_KEY = "copilot-pinned-questions";
const MAX_PINNED = 10;

function newId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) return crypto.randomUUID();
  return `s-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// The session id lives in sessionStorage: a reload of the same tab keeps the
// conversation (rehydrated from the backend, which owns the real state), while a new
// tab - or "New chat" - starts fresh. Storage can throw (private mode), so every access
// is guarded and falls back to an in-memory id.
function loadSessionId(): string {
  try {
    const existing = window.sessionStorage.getItem(SESSION_KEY);
    if (existing) return existing;
    const id = newId();
    window.sessionStorage.setItem(SESSION_KEY, id);
    return id;
  } catch {
    return newId();
  }
}

function saveSessionId(id: string) {
  try {
    window.sessionStorage.setItem(SESSION_KEY, id);
  } catch {
    /* ignore */
  }
}

interface PinnedQuestion {
  id: string;
  question: string;
}

function loadPinned(): PinnedQuestion[] {
  try {
    const parsed = JSON.parse(window.localStorage.getItem(PINNED_STORAGE_KEY) ?? "[]");
    return Array.isArray(parsed) ? parsed : [];
  } catch {
    return [];
  }
}

function savePinned(list: PinnedQuestion[]) {
  try {
    window.localStorage.setItem(PINNED_STORAGE_KEY, JSON.stringify(list));
  } catch {
    /* ignore */
  }
}

function emptyTurn(question: string): Turn {
  return {
    id: newId(),
    question,
    steps: [],
    startedAt: Date.now(),
    thinkingMs: null,
    sql: null,
    isTruncated: false,
    narrative: "",
    visualization: null,
    metadata: null,
    responseType: null,
    suggestions: [],
    error: null,
    stopped: false,
    finished: false,
  };
}

export function ChatStream() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [pinned, setPinned] = useState<PinnedQuestion[]>([]);
  const [examples, setExamples] = useState<string[]>([]);
  const [question, setQuestion] = useState("");
  const [isStreaming, setIsStreaming] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const stickToBottom = useRef(true);

  // --- boot: session id, rehydrate history, pins, example questions ----------
  useEffect(() => {
    const id = loadSessionId();
    setSessionId(id);
    setPinned(loadPinned());
    void fetchCapabilities(API_URL).then((c) => c && setExamples(c.examples));
    void fetchHistory(API_URL, id).then((history) => {
      if (history.length === 0) return;
      setTurns(
        history.map((h, i) => ({
          ...emptyTurn(h.question),
          id: `h-${i}`,
          startedAt: null,
          sql: h.sql,
          narrative: h.narrative ?? "",
          visualization: h.visualization,
          responseType: h.response_type,
          suggestions: h.suggestions ?? [],
          finished: true,
        }))
      );
    });
  }, []);

  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, MAX_TEXTAREA_HEIGHT_PX)}px`;
  }, [question]);

  // Follow the stream only while the user is already at the bottom - scrolling up to
  // read an earlier answer must not get yanked back down by every token.
  useEffect(() => {
    const onScroll = () => {
      const distance = document.documentElement.scrollHeight - window.innerHeight - window.scrollY;
      stickToBottom.current = distance < 160;
    };
    window.addEventListener("scroll", onScroll, { passive: true });
    return () => window.removeEventListener("scroll", onScroll);
  }, []);

  useEffect(() => {
    if (stickToBottom.current) bottomRef.current?.scrollIntoView({ block: "end" });
  }, [turns]);

  // --- pins ------------------------------------------------------------------
  const isPinned = (text: string) => pinned.some((p) => p.question === text);

  function togglePin(text: string) {
    setPinned((prev) => {
      const next = prev.some((p) => p.question === text)
        ? prev.filter((p) => p.question !== text)
        : [...prev, { id: newId(), question: text }].slice(-MAX_PINNED);
      savePinned(next);
      return next;
    });
  }

  function reuseQuestion(text: string) {
    setQuestion(text);
    textareaRef.current?.focus();
  }

  // --- streaming -----------------------------------------------------------
  const patchTurn = useCallback((id: string, fn: (t: Turn) => Partial<Turn>) => {
    setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, ...fn(t) } : t)));
  }, []);

  async function send(text: string) {
    const q = text.trim();
    if (!q || isStreaming || !sessionId) return;

    const turn = emptyTurn(q);
    const id = turn.id;
    setTurns((prev) => [...prev, turn]);
    setQuestion("");
    setIsStreaming(true);
    stickToBottom.current = true;

    const controller = new AbortController();
    abortRef.current = controller;

    try {
      for await (const frame of streamQuery(API_URL, q, sessionId, controller.signal)) {
        switch (frame.event) {
          case "step":
            patchTurn(id, (t) => {
              const idx = t.steps.findIndex((s) => s.id === frame.data.id);
              const steps = [...t.steps];
              if (idx === -1) steps.push(frame.data);
              else steps[idx] = { ...steps[idx], ...frame.data };
              return { steps };
            });
            break;
          case "sql":
            patchTurn(id, () => ({ sql: frame.data.sql_executed, isTruncated: frame.data.is_truncated }));
            break;
          case "narrative_delta":
            patchTurn(id, (t) => ({
              narrative: t.narrative + frame.data.delta,
              thinkingMs: t.thinkingMs ?? (t.startedAt ? Date.now() - t.startedAt : null),
            }));
            break;
          case "visualization":
            patchTurn(id, () => ({ visualization: frame.data }));
            break;
          case "suggestions":
            patchTurn(id, () => ({ suggestions: frame.data.items, responseType: frame.data.response_type }));
            break;
          case "metadata":
            patchTurn(id, () => ({ metadata: frame.data, responseType: frame.data.response_type }));
            break;
          case "error":
            patchTurn(id, () => ({ error: frame.data.message, finished: true }));
            break;
          case "done":
            patchTurn(id, () => ({ finished: true, responseType: frame.data.response_type }));
            break;
        }
      }
    } catch (err) {
      if (controller.signal.aborted) {
        patchTurn(id, () => ({ stopped: true }));
      } else {
        patchTurn(id, () => ({
          error: "Couldn't reach the assistant. Check your connection and try again.",
        }));
      }
    } finally {
      patchTurn(id, (t) => ({
        finished: true,
        thinkingMs: t.thinkingMs ?? (t.startedAt ? Date.now() - t.startedAt : null),
      }));
      abortRef.current = null;
      setIsStreaming(false);
      textareaRef.current?.focus();
    }
  }

  function stop() {
    abortRef.current?.abort();
  }

  async function newChat() {
    if (isStreaming) stop();
    if (sessionId) await clearSession(API_URL, sessionId);
    const id = newId();
    saveSessionId(id);
    setSessionId(id);
    setTurns([]);
    setQuestion("");
    textareaRef.current?.focus();
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    // key/code/keyCode all checked: IME composition (keyCode 229) must not send, while
    // hardware keyboards, remote desktops and automation tools report Enter differently.
    const isEnter = e.key === "Enter" || e.code === "Enter" || e.code === "NumpadEnter" || e.keyCode === 13;
    if (isEnter && !e.shiftKey && !e.nativeEvent.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      void send(question);
    }
  }

  const lastId = turns.length ? turns[turns.length - 1].id : null;

  return (
    <div>
      <aside className="pinned-sidebar" aria-label="Pinned questions">
        <p className="pinned-sidebar-title">Pinned questions</p>
        {pinned.length === 0 ? (
          <p className="pinned-empty">Hover a question in the chat and click the pin to keep it here.</p>
        ) : (
          <ul className="pinned-list">
            {pinned.map((p) => (
              <li className="pinned-item" key={p.id} onClick={() => reuseQuestion(p.question)} title="Reuse in the input box">
                <span className="pinned-item-text">{p.question}</span>
                <button
                  type="button"
                  className="pinned-item-unpin"
                  aria-label="Unpin question"
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

      <div className="chat-toolbar">
        <button type="button" className="new-chat-btn" onClick={() => void newChat()} disabled={turns.length === 0}>
          <PlusIcon /> New chat
        </button>
      </div>

      {turns.length === 0 && (
        <div className="welcome">
          <p className="welcome-title">What would you like to know about your sales?</p>
          <p className="welcome-sub">Ask in plain English - I&apos;ll show my reasoning, the SQL I ran, and a chart.</p>
          {examples.length > 0 && (
            <div className="suggestions is-centered">
              {examples.map((ex) => (
                <button key={ex} type="button" className="suggestion-chip" onClick={() => void send(ex)}>
                  {ex}
                </button>
              ))}
            </div>
          )}
        </div>
      )}

      <div className="message-list" aria-live="polite">
        {turns.map((t) => {
          const working = !t.finished && !t.narrative && !t.error;
          const isClarification = t.responseType === "clarification";
          const showSuggestions = t.id === lastId && t.finished && t.suggestions.length > 0 && !isStreaming;
          return (
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

              <div className={`assistant-row${isClarification ? " is-clarification" : ""}`}>
                <div className={`response-card${isClarification ? " is-clarification" : ""}`}>
                  {isClarification && (
                    <div className="clarify-badge">
                      <QuestionIcon /> Follow-up question
                    </div>
                  )}

                  {(t.steps.length > 0 || working) && (
                    <ThinkingPanel
                      steps={t.steps}
                      active={working}
                      startedAt={t.startedAt}
                      finishedMs={t.thinkingMs}
                    />
                  )}
                  {working && t.steps.length === 0 && <TypingDots />}

                  {t.narrative && (
                    <div className="narrative-text">
                      <Markdown text={t.narrative} caret={!t.finished} />
                    </div>
                  )}
                  {t.error && <div className="error-text">{t.error}</div>}
                  {t.stopped && !t.error && <div className="stopped-text">Stopped.</div>}

                  {t.visualization && <ChartRenderer config={t.visualization} />}
                  <SqlAuditDrawer sql={t.sql} isTruncated={t.isTruncated} metadata={t.metadata} />
                </div>

                {showSuggestions && (
                  <div className={`suggestions${isClarification ? " is-reply" : ""}`}>
                    {t.suggestions.map((s) => (
                      <button key={s} type="button" className="suggestion-chip" onClick={() => void send(s)}>
                        {s}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
          );
        })}
        <div ref={bottomRef} className="scroll-sentinel" aria-hidden="true" />
      </div>

      <form
        className="query-form"
        onSubmit={(e) => {
          e.preventDefault();
          void send(question);
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
            placeholder={isStreaming ? "Working on it…" : "Ask about sales, e.g. 'total revenue by division in 2020'"}
            maxLength={1000}
            aria-label="Your question"
          />
          {isStreaming ? (
            <button className="query-submit is-stop" type="button" onClick={stop} aria-label="Stop" title="Stop">
              <span className="stop-square" aria-hidden="true" />
            </button>
          ) : (
            <button className="query-submit" type="submit" disabled={!question.trim()} aria-label="Ask" title="Ask">
              <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
                <path d="M12 19V5M12 5L6 11M12 5L18 11" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </button>
          )}
        </div>
        <p className="query-hint">Enter to send · Shift+Enter for a new line · Answers use only the connected sales data</p>
      </form>
    </div>
  );
}

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
      <path d="M6 6l12 12M18 6L6 18" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function PlusIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
      <path d="M12 5v14M5 12h14" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
    </svg>
  );
}

function QuestionIcon() {
  return (
    <svg viewBox="0 0 24 24" width="13" height="13" aria-hidden="true">
      <circle cx="12" cy="12" r="9" fill="none" stroke="currentColor" strokeWidth="2" />
      <path d="M9.5 9.5a2.5 2.5 0 1 1 3.5 2.3c-.7.3-1 .9-1 1.7" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" />
      <circle cx="12" cy="17" r="1.2" fill="currentColor" />
    </svg>
  );
}
