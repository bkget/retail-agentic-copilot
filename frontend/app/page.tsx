import { ChatStream } from "@/components/ChatStream";

export default function Home() {
  return (
    <main className="app-shell">
      <h1 className="app-title">Agentic Data Copilot</h1>
      <p className="app-subtitle">
        Conversational analytics over the sales semantic layer. SQL is generated,
        AST-guardrailed, and executed read-only - every answer shows its work.
      </p>
      <ChatStream />
    </main>
  );
}
