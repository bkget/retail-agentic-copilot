"use client";

import { useEffect, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Legend,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { VisualizationConfig } from "@/lib/types";

// Recharts applies tick/stroke colors as SVG presentation attributes, which don't
// reliably resolve CSS var(--x) - so concrete light/dark hex values are picked in JS.
// The categorical palette is ordered for maximum adjacent contrast.
const PALETTE_LIGHT = ["#2a78d6", "#e8793a", "#2b9e6f", "#8b5cf6", "#d6427a", "#0e9fb0", "#b58a00", "#6b7280"];
const PALETTE_DARK = ["#3987e5", "#f08c4d", "#3bb383", "#a07cf8", "#e25b8e", "#22b8c9", "#d4a514", "#9ca3af"];
const GRID_LIGHT = "#e1e0d9";
const GRID_DARK = "#2c2c2a";
const TEXT_SECONDARY_LIGHT = "#52514e";
const TEXT_SECONDARY_DARK = "#c3c2b7";
const AXIS_COLOR = "#898781";

function useIsDarkMode(): boolean {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const root = document.documentElement;
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

function formatCell(value: string | number | null | undefined, column: string, isMetric: boolean): string {
  if (value === null || value === undefined) return "–";
  if (typeof value === "number") {
    if (!isMetric || column.toLowerCase().includes("year")) return String(value);
    return value.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  return value;
}

function prettyHeader(c: string): string {
  return c.replace(/^(sale|store|item|payment)_/, "").replace(/_/g, " ");
}

export function ChartRenderer({ config }: { config: VisualizationConfig }) {
  const isDark = useIsDarkMode();
  const [view, setView] = useState<"chart" | "table">("chart");
  const palette = isDark ? PALETTE_DARK : PALETTE_LIGHT;
  const gridColor = isDark ? GRID_DARK : GRID_LIGHT;
  const textSecondary = isDark ? TEXT_SECONDARY_DARK : TEXT_SECONDARY_LIGHT;

  if (!config.render_chart) return null;
  const { title, chart_type, data, columns } = config;
  const canToggle = chart_type !== "table" && columns.length > 0;
  const showTable = chart_type === "table" || view === "table";
  // First column(s) are dimensions; for multi_line every column after the x key is a
  // metric series.
  const metricCols = new Set(
    chart_type === "multi_line" ? columns.slice(1) : columns.slice(-1)
  );

  const header = (
    <div className="viz-header">
      {title && (
        <div className="viz-title" style={{ color: textSecondary }}>
          {title}
        </div>
      )}
      {canToggle && (
        <div className="viz-toggle" role="tablist" aria-label="View as">
          <button type="button" className={view === "chart" ? "is-active" : ""} onClick={() => setView("chart")}>
            Chart
          </button>
          <button type="button" className={view === "table" ? "is-active" : ""} onClick={() => setView("table")}>
            Table
          </button>
        </div>
      )}
    </div>
  );

  if (showTable) {
    return (
      <div className="viz">
        {header}
        <div className="viz-table-wrap">
          <table className="viz-table">
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c}>{prettyHeader(c)}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.map((row, i) => (
                <tr key={i}>
                  {columns.map((c) => (
                    <td key={c}>{formatCell(row[c], c, metricCols.has(c))}</td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    );
  }

  if (!config.x_axis_key) return null;
  const xKey = config.x_axis_key;
  const axisProps = {
    tick: { fill: AXIS_COLOR, fontSize: 12 },
    axisLine: false as const,
    tickLine: false as const,
    tickFormatter: formatTick,
    width: 48,
  };
  const tooltip = (
    <Tooltip
      formatter={(value: number) => formatTick(value)}
      contentStyle={{ fontSize: 12, borderRadius: 8 }}
    />
  );

  if (chart_type === "multi_line") {
    const series = config.series_keys ?? columns.slice(1);
    return (
      <div className="viz">
        {header}
        <ResponsiveContainer width="100%" height={300}>
          <LineChart data={data} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid stroke={gridColor} vertical={false} />
            <XAxis dataKey={xKey} tick={{ fill: AXIS_COLOR, fontSize: 12 }} axisLine={{ stroke: gridColor }} tickLine={false} />
            <YAxis {...axisProps} />
            {tooltip}
            <Legend wrapperStyle={{ fontSize: 12, paddingTop: 6 }} iconType="plainline" />
            {series.map((s, i) => (
              <Line
                key={s}
                type="monotone"
                dataKey={s}
                name={s}
                stroke={palette[i % palette.length]}
                strokeWidth={2}
                dot={data.length <= 12 ? { r: 3, fill: palette[i % palette.length] } : false}
                connectNulls
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    );
  }

  if (!config.y_axis_key) return null;
  const yKey = config.y_axis_key;
  const seriesColor = palette[0];

  return (
    <div className="viz">
      {header}
      <ResponsiveContainer width="100%" height={280}>
        {chart_type === "line" ? (
          <LineChart data={data} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid stroke={gridColor} vertical={false} />
            <XAxis dataKey={xKey} tick={{ fill: AXIS_COLOR, fontSize: 12 }} axisLine={{ stroke: gridColor }} tickLine={false} />
            <YAxis {...axisProps} />
            {tooltip}
            <Line type="monotone" dataKey={yKey} stroke={seriesColor} strokeWidth={2} dot={{ r: 3, fill: seriesColor }} />
          </LineChart>
        ) : (
          <BarChart data={data} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid stroke={gridColor} vertical={false} />
            <XAxis
              dataKey={xKey}
              tick={{ fill: AXIS_COLOR, fontSize: 11 }}
              axisLine={{ stroke: gridColor }}
              tickLine={false}
              interval={0}
              angle={data.length > 6 ? -30 : 0}
              textAnchor={data.length > 6 ? "end" : "middle"}
              height={data.length > 6 ? 56 : 30}
            />
            <YAxis {...axisProps} />
            {tooltip}
            <Bar dataKey={yKey} fill={seriesColor} radius={[4, 4, 0, 0]} maxBarSize={48} />
          </BarChart>
        )}
      </ResponsiveContainer>
    </div>
  );
}
