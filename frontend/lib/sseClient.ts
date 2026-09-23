import type { Capabilities, HistoryTurn, SseFrame } from "./types";

// Browser EventSource can't POST a body, so the stream is read manually via fetch +
// ReadableStream. Frames are `event: <name>\ndata: <json>\n\n`; comment lines
// (": heartbeat") carry no event and are skipped by parseFrame.
export async function* streamQuery(
  apiUrl: string,
  question: string,
  sessionId: string,
  signal?: AbortSignal
): AsyncGenerator<SseFrame> {
  const response = await fetch(`${apiUrl}/api/query`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question, session_id: sessionId }),
    signal,
  });

  if (response.status === 429) {
    const body = await response.json().catch(() => ({ error: "Rate limit exceeded." }));
    yield { event: "error", data: { message: body.error ?? "Rate limit exceeded." } };
    return;
  }
  if (response.status === 422) {
    yield { event: "error", data: { message: "That message couldn't be processed - it may be empty or too long." } };
    return;
  }
  if (!response.ok || !response.body) {
    yield { event: "error", data: { message: `Request failed (${response.status}).` } };
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let separatorIndex: number;
    while ((separatorIndex = buffer.indexOf("\n\n")) !== -1) {
      const rawFrame = buffer.slice(0, separatorIndex);
      buffer = buffer.slice(separatorIndex + 2);
      const frame = parseFrame(rawFrame);
      if (frame) yield frame;
    }
  }
}

function parseFrame(raw: string): SseFrame | null {
  let eventName: string | null = null;
  let dataLine: string | null = null;
  for (const line of raw.split("\n")) {
    if (line.startsWith("event: ")) eventName = line.slice("event: ".length).trim();
    else if (line.startsWith("data: ")) dataLine = line.slice("data: ".length);
  }
  if (!eventName || dataLine === null) return null;
  try {
    return { event: eventName, data: JSON.parse(dataLine) } as SseFrame;
  } catch {
    return null;
  }
}

export async function fetchHistory(apiUrl: string, sessionId: string): Promise<HistoryTurn[]> {
  try {
    const res = await fetch(`${apiUrl}/api/session/${encodeURIComponent(sessionId)}/history`);
    if (!res.ok) return [];
    const body = await res.json();
    return Array.isArray(body.turns) ? body.turns : [];
  } catch {
    return [];
  }
}

export async function clearSession(apiUrl: string, sessionId: string): Promise<void> {
  try {
    await fetch(`${apiUrl}/api/session/${encodeURIComponent(sessionId)}`, { method: "DELETE" });
  } catch {
    /* best effort - a fresh session id is used either way */
  }
}

export async function fetchCapabilities(apiUrl: string): Promise<Capabilities | null> {
  try {
    const res = await fetch(`${apiUrl}/api/capabilities`);
    return res.ok ? await res.json() : null;
  } catch {
    return null;
  }
}
