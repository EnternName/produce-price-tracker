// Price history line chart: one series per store, colours follow the store (stable slot), not rank.
(function () {
  const el = document.getElementById("series-data");
  const canvas = document.getElementById("history");
  if (!el || !canvas || !window.Chart) return;
  const series = JSON.parse(el.textContent);
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  const day = (iso) => new Date(iso + "T00:00:00").getTime();
  const fmtDate = (ms) => new Date(ms).toLocaleDateString(undefined, { day: "numeric", month: "short", year: "2-digit" });
  const fmtEur = (v) => v.toLocaleString("es-ES", { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + " €";

  // Vertical hairline that snaps to the hovered date.
  const crosshair = {
    id: "crosshair",
    afterDatasetsDraw(chart) {
      const active = chart.tooltip && chart.tooltip.getActiveElements();
      if (!active || !active.length) return;
      const x = active[0].element.x;
      const { top, bottom } = chart.chartArea;
      const ctx = chart.ctx;
      ctx.save();
      ctx.strokeStyle = css("--axis");
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, top);
      ctx.lineTo(x, bottom);
      ctx.stroke();
      ctx.restore();
    },
  };

  function build() {
    const surface = css("--surface");
    const datasets = series.map((s) => {
      const color = css(s.slot ? `--series-${s.slot}` : "--series-other");
      return {
        label: s.store,
        data: s.points.map((p) => ({ x: day(p.x), y: p.y })),
        borderColor: color,
        backgroundColor: color,
        borderWidth: 2,
        borderDash: s.slot ? [] : [4, 3],
        pointRadius: 4,
        pointHoverRadius: 6,
        pointBorderColor: surface,
        pointBorderWidth: 2,
        pointHitRadius: 12,
        tension: 0,
      };
    });
    return new Chart(canvas, {
      type: "line",
      data: { datasets },
      plugins: [crosshair],
      options: {
        maintainAspectRatio: false,
        animation: false,
        interaction: { mode: "nearest", axis: "x", intersect: false },
        scales: {
          x: {
            type: "linear",
            ticks: { color: css("--muted"), maxTicksLimit: 8, callback: (v) => fmtDate(v) },
            grid: { display: false },
            border: { color: css("--axis") },
          },
          y: {
            ticks: { color: css("--muted"), callback: (v) => fmtEur(v) },
            grid: { color: css("--grid") },
            border: { display: false },
          },
        },
        plugins: {
          legend: {
            display: series.length > 1,
            position: "top",
            align: "start",
            labels: {
              color: css("--text-2"),
              usePointStyle: true,
              boxWidth: 18,
              // Key each store with a short stroke of its line colour (the default uses the point ring colour).
              generateLabels: (chart) =>
                chart.data.datasets.map((ds, i) => ({
                  text: ds.label,
                  datasetIndex: i,
                  hidden: !chart.isDatasetVisible(i),
                  pointStyle: "line",
                  strokeStyle: ds.borderColor,
                  fillStyle: ds.borderColor,
                  lineWidth: 3,
                  lineDash: ds.borderDash,
                  fontColor: css("--text-2"),
                })),
            },
          },
          tooltip: {
            backgroundColor: css("--surface"),
            titleColor: css("--text-2"),
            bodyColor: css("--text"),
            borderColor: css("--axis"),
            borderWidth: 1,
            usePointStyle: true,
            callbacks: {
              title: (items) => (items.length ? fmtDate(items[0].parsed.x) : ""),
              label: (item) => `${fmtEur(item.parsed.y)}  ${item.dataset.label}`,
              labelPointStyle: () => ({ pointStyle: "line", rotation: 0 }),
              labelColor: (item) => ({ borderColor: item.dataset.borderColor, backgroundColor: item.dataset.borderColor, borderWidth: 3 }),
            },
          },
        },
      },
    });
  }

  let chart = build();
  // Re-read colour tokens when the OS theme flips.
  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    chart.destroy();
    chart = build();
  });
})();
