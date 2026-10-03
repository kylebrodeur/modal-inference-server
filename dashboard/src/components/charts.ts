// 24h charts: Arrow component shells + Chart.js canvases inside.
// The Arrow template renders once per slot; Chart.js handles are created at
// mount and only .update() their data on refresh ticks (no re-instantiation).
import { Chart } from "chart.js/auto";
import { html } from "@arrow-js/core";
import type { HourBucket } from "../lib/analytics.js";

const GRID = "#303746";
const TICK = "#9aa4b8";
const NO_DATA = "#46516a88";

export interface BarSpec {
  label: string;
  color: string;
  // Returns the value for a bucket, or null for "no data" (rendered as a gap).
  accessor: (b: HourBucket) => number | null;
}

const SPECS: { id: string; spec: BarSpec; unit: string }[] = [
  { id: "chart-requests", unit: "requests", spec: { label: "requests / hour", color: "#6ea8ff", accessor: b => b.req || null } },
  {
    id: "chart-rate",
    unit: "tok/s",
    spec: {
      label: "generation tok/s (avg per request)",
      color: "#6ee7a8",
      accessor: b => {
        if (!b.rates.length) return null;
        const sum = b.rates.reduce((a: number, v: number) => a + v, 0);
        return sum / b.rates.length;
      },
    },
  },
];

const chartData = (buckets: HourBucket[], spec: BarSpec) => ({
  labels: buckets.map(b => new Date(b.t).toLocaleTimeString([], { hour: "2-digit" })),
  datasets: [
    {
      label: spec.label,
      data: buckets.map(b => spec.accessor(b)),
      backgroundColor: buckets.map(b => (spec.accessor(b) == null ? NO_DATA : spec.color)),
      borderWidth: 0,
      borderRadius: 2,
      categoryPercentage: 0.9,
      barPercentage: 0.85,
    },
  ],
});

const baseOptions = (unit: string) =>
  ({
    responsive: true,
    maintainAspectRatio: false,
    animation: false as const,
    plugins: {
      legend: { display: false },
      tooltip: {
        backgroundColor: "#171b24",
        borderColor: GRID,
        borderWidth: 1,
        titleColor: "#edf0f7",
        bodyColor: TICK,
        callbacks: {
          label: (ctx: { parsed: { y: number | null } }) =>
            ctx.parsed.y == null ? "no data" : `${ctx.parsed.y.toFixed(1)} ${unit}`,
        },
      },
    },
    scales: {
      x: { grid: { display: false }, ticks: { color: TICK, maxRotation: 0, autoSkip: true, maxTicksLimit: 6, font: { size: 9 } } },
      y: { beginAtZero: true, grid: { color: GRID }, ticks: { color: TICK, font: { size: 9 }, maxTicksLimit: 4 } },
    },
  }) as const;

export interface ChartPairHandle {
  update: (buckets: HourBucket[]) => void;
  dispose: () => void;
}

// Arrow component: mounts the section shell with both chart panels (skeletons
// keep layout height), then lazily creates the Chart.js canvases on first
// update() with data. update() is cheap afterwards.
export const ChartPair = (mountRoot: HTMLElement): ChartPairHandle => {
  mountRoot.replaceChildren();
  html`<div>
    <h2>Last 24 hours</h2>
    <div class="charts">
      <div class="chart panel"><h3>${SPECS[0].spec.label}</h3><div class="chart-box"><canvas id="chart-requests"></canvas></div></div>
      <div class="chart panel"><h3>${SPECS[1].spec.label}</h3><div class="chart-box"><canvas id="chart-rate"></canvas></div></div>
    </div>
  </div>`(mountRoot);

  let charts: Chart[] | null = null;
  return {
    update: (buckets: HourBucket[]) => {
      if (!charts) {
        charts = SPECS.map(({ id, spec, unit }) => {
          const canvas = mountRoot.querySelector<HTMLCanvasElement>(`#${id}`);
          if (!canvas) throw new Error(`missing chart canvas #${id}`);
          return new Chart(canvas, {
            type: "bar",
            data: chartData(buckets, spec),
            options: baseOptions(unit),
          });
        });
      } else {
        SPECS.forEach(({ spec }, i) => {
          const c = charts![i];
          c.data = chartData(buckets, spec);
          c.update("none");
        });
      }
    },
    dispose: () => {
      charts?.forEach(c => c.destroy());
      charts = null;
      mountRoot.replaceChildren();
    },
  };
};