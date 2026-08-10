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
const SERIES_LIGHT = "#2a78d6";
const SERIES_DARK = "#3987e5";
const GRID_COLOR = "#e1e0d9";
const AXIS_COLOR = "#898781";
const TEXT_SECONDARY = "#52514e";

function useIsDarkMode(): boolean {
  const [isDark, setIsDark] = useState(false);
  useEffect(() => {
    const query = window.matchMedia("(prefers-color-scheme: dark)");
    setIsDark(query.matches);
    const listener = (e: MediaQueryListEvent) => setIsDark(e.matches);
    query.addEventListener("change", listener);
    return () => query.removeEventListener("change", listener);
  }, []);
  return isDark;
}

function formatTick(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${(value / 1_000).toFixed(0)}K`;
  return `${value}`;
}

export function ChartRenderer({ config }: { config: VisualizationConfig }) {
  const isDark = useIsDarkMode();
  const seriesColor = isDark ? SERIES_DARK : SERIES_LIGHT;

  if (!config.render_chart || !config.x_axis_key || !config.y_axis_key) return null;

  const { x_axis_key: xKey, y_axis_key: yKey, data, title, chart_type } = config;

  return (
    <div style={{ marginTop: 16 }}>
      {title && (
        <div style={{ fontSize: 13, color: TEXT_SECONDARY, marginBottom: 8, fontWeight: 600 }}>
          {title}
        </div>
      )}
      <ResponsiveContainer width="100%" height={280}>
        {chart_type === "line" ? (
          <LineChart data={data} margin={{ top: 4, right: 12, left: 0, bottom: 4 }}>
            <CartesianGrid stroke={GRID_COLOR} vertical={false} strokeWidth={1} />
            <XAxis
              dataKey={xKey}
              tick={{ fill: AXIS_COLOR, fontSize: 12 }}
              axisLine={{ stroke: GRID_COLOR }}
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
            <CartesianGrid stroke={GRID_COLOR} vertical={false} strokeWidth={1} />
            <XAxis
              dataKey={xKey}
              tick={{ fill: AXIS_COLOR, fontSize: 11 }}
              axisLine={{ stroke: GRID_COLOR }}
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
