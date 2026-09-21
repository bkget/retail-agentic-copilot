"use client";

import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { VisualizationConfig } from "@/lib/types";

// Single-series slot 1 from the validated categorical palette (see dataviz skill /
// references/palette.md). Every chart this app renders is a single measure grouped by
// one dimension, so one series color is always correct here - a legend would be
// redundant (the chart title already names the series).
// Mirrors the --series-1/--gridline/--text-secondary tokens in globals.css. Recharts
// applies `tick={{ fill: ... }}` as a plain SVG presentation attribute, not a CSS
// `style` property, and those don't reliably resolve `var(--x)` - so these have to stay
// concrete hex pairs picked in JS by theme, not CSS custom properties.
const SERIES_LIGHT = "#2a78d6";
const SERIES_DARK = "#3987e5";
const GRID_LIGHT = "#e1e0d9";
const GRID_DARK = "#2c2c2a";
const TEXT_SECONDARY_LIGHT = "#52514e";
const TEXT_SECONDARY_DARK = "#c3c2b7";
// Identical in both themes by design (see globals.css --text-muted) - no light/dark pair needed.
const AXIS_COLOR = "#898781";

function useIsDarkMode(): boolean {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const root = document.documentElement;
    // Tracks the actual active theme (set by ThemeToggle / the init script in
    // layout.tsx), not just the OS's prefers-color-scheme - those diverge the moment
    // someone picks a theme that differs from their system setting, and a plain
    // media-query listener would never notice the toggle being clicked.
    const read = () => setIsDark(root.getAttribute("data-theme") === "dark");
    read();
    const observer = new MutationObserver(read);
    observer.observe(root, { attributes: true, attributeFilter: ["data-theme"] });
    return () => observer.disconnect();
  }, []);
  return isDark;
}

function formatTick(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(0)}K`;
  return `${value}`;
}

// Full precision with thousands separators, not the abbreviated K/M chart-tick format -
// a table is the exact-values view, so it should show exact values. Year-like columns
// are deliberately excluded from grouping/decimals ("2,019" reads as a typo, not a
// year), and every other numeric column gets a fixed 2 decimal places so amounts in
// the same column line up instead of some rows showing ".5" and others showing none.
function formatCell(value: string | number | null, column: string): string {
  if (value === null) return "";
  if (typeof value === "number") {
    if (column.toLowerCase().includes("year")) return String(value);
    return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  return value;
}

export function ChartRenderer({ config }: { config: VisualizationConfig }) {
  const isDark = useIsDarkMode();
  const seriesColor = isDark ? SERIES_DARK : SERIES_LIGHT;
  const gridColor = isDark ? GRID_DARK : GRID_LIGHT;
  const textSecondary = isDark ? TEXT_SECONDARY_DARK : TEXT_SECONDARY_LIGHT;

  if (!config.render_chart) return null;

  const { title, chart_type, data } = config;

  const titleEl = title && (
    <div style={{ fontSize: 13, color: textSecondary, marginBottom: 8, fontWeight: 600 }}>{title}</div>
  );

  // A result with 2+ independent dimension columns (e.g. a multi-year comparison also
  // broken down by quarter) can't be represented as a single x/y chart without
  // collapsing dimensions into one combined label that reads as a flat trend line
  // rather than a real comparison - a table shows every value precisely instead (see
  // app/agent/visualization.py for the same reasoning on the backend side).
  if (chart_type === "table") {
    return (
      <div style={{ marginTop: 16 }}>
        {titleEl}
        <div className="viz-table-wrap">
          <table className="viz-table">
            <thead>
              <tr>
                {config.columns.map((c) => (
                  <th key={c}>{c.replace(/_/g, " ")}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.map((row, i) => (
                <tr key={i}>
                  {config.columns.map((c) => (
                    <td key={c}>{formatCell(row[c], c)}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  if (!config.x_axis_key || !config.y_axis_key) return null;
  const { x_axis_key: xKey, y_axis_key: yKey } = config;

  return (
    <div style={{ marginTop: 16 }}>
      {titleEl}
      <ResponsiveContainer width="100%" height={280}>
        {chart_type === "line" ? (
          <LineChart data={data} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid stroke={gridColor} vertical={false} strokeWidth={1} />
            <XAxis
              dataKey={xKey}
              tick={{ fill: AXIS_COLOR, fontSize: 12 }}
              axisLine={{ stroke: gridColor }}
              tickLine={false}
            />
            <YAxis
              tick={{ fill: AXIS_COLOR, fontSize: 12 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={formatTick}
              width={48}
            />
            <Tooltip
              formatter={(value: number) => formatTick(value)}
              contentStyle={{ fontSize: 12, borderRadius: 8 }}
            />
            <Line
              type="monotone"
              dataKey={yKey}
              stroke={seriesColor}
              strokeWidth={2}
              dot={{ r: 4, fill: seriesColor }}
            />
          </LineChart>
        ) : (
          <BarChart data={data} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid stroke={gridColor} vertical={false} strokeWidth={1} />
            <XAxis
              dataKey={xKey}
              tick={{ fill: AXIS_COLOR, fontSize: 11 }}
              axisLine={{ stroke: gridColor }}
              tickLine={false}
              interval={0}
              angle={data.length > 6 ? -30 : 0}
              textAnchor={data.length > 6 ? "end" : "middle"}
              height={data.length > 6 ? 50 : 30}
            />
            <YAxis
              tick={{ fill: AXIS_COLOR, fontSize: 12 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={formatTick}
              width={48}
            />
            <Tooltip
              formatter={(value: number) => formatTick(value)}
              contentStyle={{ fontSize: 12, borderRadius: 8 }}
            />
            <Bar dataKey={yKey} fill={seriesColor} radius={[4, 4, 0, 0]} maxBarSize={48} />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
