import type { SseFrame } from "./types";

// Browser EventSource can't send a POST body, and this endpoint needs one (the
// question + session_id), so we read the stream manually via fetch + ReadableStream
// instead. Frames are `event: <name>\ndata: <json>\n\n` - split on the blank-line
// separator, parse each line pair.
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
    const data = JSON.parse(dataLine);
    return { event: eventName, data } as SseFrame;
  } catch {
    return null;
  }
}
