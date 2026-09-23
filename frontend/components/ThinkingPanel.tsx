"use client";

import { useEffect, useState } from "react";
import type { ThinkingStep } from "@/lib/types";

/**
 * The live reasoning trace, modeled on the "Thinking..." affordance in modern chat
 * assistants: expanded with a shimmering header while the agent works, then collapsed
 * to a one-line "Thought for 1.4s" summary once the answer starts streaming. Every
 * line comes from a real backend stage (intent, context, plan, SQL, guardrail,
 * execution) - nothing is simulated on the client.
 */
export function ThinkingPanel({
  steps,
  active,
  startedAt,
  finishedMs,
}: {
  steps: ThinkingStep[];
  active: boolean; // still working (no answer text yet)
  startedAt: number | null;
  finishedMs: number | null;
}) {
  const [open, setOpen] = useState(active);
  const [now, setNow] = useState(() => Date.now());

  // Auto-expand while working, auto-collapse when the answer begins - but only on
  // that transition, so a user who re-opens it afterwards keeps it open.
  useEffect(() => setOpen(active), [active]);

  useEffect(() => {
    if (!active) return;
    const t = setInterval(() => setNow(Date.now()), 100);
    return () => clearInterval(t);
  }, [active]);

  if (steps.length === 0 && !active) return null;

  const elapsedMs = active && startedAt ? now - startedAt : finishedMs ?? 0;
  const seconds = (elapsedMs / 1000).toFixed(1);
  const current = [...steps].reverse().find((s) => s.status === "running") ?? steps[steps.length - 1];

  return (
    <div className={`thinking${active ? " is-active" : ""}`}>
      <button
        type="button"
        className="thinking-header"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
      >
        <span className="thinking-icon" aria-hidden="true">
          {active ? <span className="thinking-spinner" /> : <SparkIcon />}
        </span>
        <span className={active ? "thinking-title shimmer" : "thinking-title"}>
          {active ? (current ? `${current.label}…` : "Thinking…") : `Thought for ${seconds}s`}
        </span>
        {active && <span className="thinking-timer">{seconds}s</span>}
        <ChevronIcon open={open} />
      </button>

      {open && steps.length > 0 && (
        <ol className="thinking-steps">
          {steps.map((s) => (
            <li key={s.id} className={`thinking-step is-${s.status}`}>
              <span className="thinking-step-marker" aria-hidden="true">
                {s.status === "running" ? (
                  <span className="thinking-step-pulse" />
                ) : s.status === "error" ? (
                  "!"
                ) : s.status === "warning" ? (
                  "•"
                ) : (
                  <CheckIcon />
                )}
              </span>
              <div className="thinking-step-body">
                <span className="thinking-step-label">{s.label}</span>
                {s.detail && <span className="thinking-step-detail">{s.detail}</span>}
              </div>
              {s.elapsed_ms != null && s.elapsed_ms > 0 && (
                <span className="thinking-step-time">{s.elapsed_ms} ms</span>
              )}
            </li>
          ))}
        </ol>
      )}
    </div>
  );
}

/** Three bouncing dots - shown in the brief gap before the first step arrives. */
export function TypingDots() {
  return (
    <span className="typing-dots" role="status" aria-label="Assistant is typing">
      <span />
      <span />
      <span />
    </span>
  );
}

function ChevronIcon({ open }: { open: boolean }) {
  return (
    <svg
      className="thinking-chevron"
      viewBox="0 0 24 24"
      width="14"
      height="14"
      aria-hidden="true"
      style={{ transform: open ? "rotate(90deg)" : "none" }}
    >
      <path d="M9 6l6 6-6 6" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
      <path d="M5 12.5l4.5 4.5L19 7.5" fill="none" stroke="currentColor" strokeWidth="2.6" strokeLinecap="round" strokeLinejoin="round" />
    </svg>
  );
}

function SparkIcon() {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" aria-hidden="true">
      <path
        d="M12 3l1.8 5.2L19 10l-5.2 1.8L12 17l-1.8-5.2L5 10l5.2-1.8L12 3z"
        fill="currentColor"
      />
    </svg>
  );
}
