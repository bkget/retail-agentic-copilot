import type { Metadata } from "next";
import Script from "next/script";
import "./globals.css";
import { ThemeToggle, THEME_INIT_SCRIPT } from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Agentic Data Copilot",
  description: "Conversational analytics on PostgreSQL",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // The theme-init script (below) stamps data-theme on <html> before React hydrates,
  // by design - the server never knows the client's saved preference.
  // suppressHydrationWarning tells React that specific, expected mismatch is fine,
  // without silencing hydration warnings anywhere else in the tree.
  return (
    <html lang="en" suppressHydrationWarning>
      <body>
        {/* Runs before hydration so the page never paints in the wrong theme first -
            reads the saved preference (or system default) and stamps data-theme on
            <html> synchronously, matching the CSS tokens in globals.css. */}
        <Script id="theme-init" strategy="beforeInteractive">
          {THEME_INIT_SCRIPT}
        </Script>
        <ThemeToggle />
        {children}
      </body>
    </html>
  );
}
