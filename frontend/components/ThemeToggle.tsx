"use client";

import { useEffect, useState } from "react";

type Theme = "light" | "dark";

export function ThemeToggle() {
  // The blocking script in layout.tsx already set data-theme on <html> before this
  // component ever mounts (avoids a flash of the wrong theme) - this just mirrors that
  // into React state so the buttons can show which one is active. Rendering null until
  // mounted avoids a server/client markup mismatch, since the real value only exists
  // in the browser (localStorage / prefers-color-scheme).
  const [theme, setTheme] = useState<Theme | null>(null);

  useEffect(() => {
    const current = document.documentElement.getAttribute("data-theme");
    setTheme(current === "dark" ? "dark" : "light");
  }, []);

  function select(next: Theme) {
    setTheme(next);
    document.documentElement.setAttribute("data-theme", next);
    window.localStorage.setItem("theme", next);
  }

  if (!theme) return null;

  return (
    <div className="theme-toggle" role="group" aria-label="Color theme">
      <button
        type="button"
        className={`theme-toggle-btn ${theme === "light" ? "is-active" : ""}`}
        onClick={() => select("light")}
        aria-pressed={theme === "light"}
        title="Light theme"
      >
        <span aria-hidden="true">☀️</span>
        <span className="theme-toggle-label">Light</span>
      </button>
      <button
        type="button"
        className={`theme-toggle-btn ${theme === "dark" ? "is-active" : ""}`}
        onClick={() => select("dark")}
        aria-pressed={theme === "dark"}
        title="Dark theme"
      >
        <span aria-hidden="true">🌙</span>
        <span className="theme-toggle-label">Dark</span>
      </button>
    </div>
  );
}

// Exported for layout.tsx's blocking init script, kept next to the logic it must stay
// consistent with rather than duplicated inline in a template string by hand.
export const THEME_INIT_SCRIPT = `
(function () {
  try {
    var stored = localStorage.getItem('theme');
    var theme = stored === 'light' || stored === 'dark'
      ? stored
      : (window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    document.documentElement.setAttribute('data-theme', theme);
  } catch (e) {}
})();
`;
