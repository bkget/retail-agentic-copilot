import { Fragment, ReactNode } from "react";

/**
 * Minimal, XSS-safe markdown for assistant replies: paragraphs, "- " bullet lists and
 * **bold**. Built from React nodes (never innerHTML), so model or data text can't
 * inject markup. `caret` appends the blinking streaming cursor to the last block.
 */
export function Markdown({ text, caret = false }: { text: string; caret?: boolean }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="md">
      {blocks.map((block, i) => {
        const isLast = i === blocks.length - 1;
        const lines = block.split("\n");
        const bulletLines = lines.filter((l) => /^\s*[-*]\s+/.test(l));
        const caretEl = caret && isLast ? <span className="stream-caret" aria-hidden="true" /> : null;

        if (bulletLines.length > 0 && bulletLines.length === lines.filter((l) => l.trim()).length) {
          return (
            <ul key={i}>
              {bulletLines.map((l, j) => (
                <li key={j}>
                  {inline(l.replace(/^\s*[-*]\s+/, ""))}
                  {j === bulletLines.length - 1 ? caretEl : null}
                </li>
              ))}
            </ul>
          );
        }
        // Mixed block (e.g. "You can ask me about:" followed by bullets).
        if (bulletLines.length > 0) {
          const head = lines.filter((l) => !/^\s*[-*]\s+/.test(l));
          return (
            <Fragment key={i}>
              <p>{inline(head.join(" "))}</p>
              <ul>
                {bulletLines.map((l, j) => (
                  <li key={j}>
                    {inline(l.replace(/^\s*[-*]\s+/, ""))}
                    {j === bulletLines.length - 1 ? caretEl : null}
                  </li>
                ))}
              </ul>
            </Fragment>
          );
        }
        return (
          <p key={i}>
            {lines.map((l, j) => (
              <Fragment key={j}>
                {j > 0 && <br />}
                {inline(l)}
              </Fragment>
            ))}
            {caretEl}
          </p>
        );
      })}
    </div>
  );
}

function inline(text: string): ReactNode[] {
  return text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? (
      <strong key={i}>{part.slice(2, -2)}</strong>
    ) : (
      <Fragment key={i}>{part}</Fragment>
    )
  );
}
