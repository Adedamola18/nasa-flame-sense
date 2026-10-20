"use client";

import * as Plot from "@observablehq/plot";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";
import { overpass, solarTime, type Formatter, type Satellite } from "@/lib/data";
import { PlotFigure } from "./PlotFigure";

type Series = { name: Satellite; color: string };

function panel(series: Series[], format: Formatter) {
  const rows = series.flatMap((s) => overpass[s.name].map((d) => ({ ...d, name: s.name })));
  const hours = rows.map((d) => d.hour);
  const domain = [Math.min(...hours) - 0.08, Math.max(...hours) + 0.12];
  return (): Plot.PlotOptions => ({
    marginLeft: 44,
    marginRight: 14,
    marginBottom: 22,
    x: { label: null, tickFormat: (d) => String(d), ticks: [2003, 2012, 2024] },
    y: { label: null, domain, grid: true, ticks: 3, tickFormat: (h) => solarTime(format, h) },
    color: { domain: series.map((s) => s.name), range: series.map((s) => s.color) },
    marks: [
      Plot.line(rows, { x: "year", y: "hour", stroke: "name", strokeWidth: 1.8 }),
      Plot.dot(rows, { x: "year", y: "hour", fill: "name", r: 1.8, title: (d) => `${d.name} ${d.year}: ${solarTime(format, d.hour)}` }),
      Plot.text(rows, Plot.selectLast({ x: "year", y: "hour", z: "name", text: "name", fill: "name", dx: -6, dy: -9, textAnchor: "end" })),
    ],
  });
}

export function OverpassDrift() {
  const t = useTranslations("Charts");
  const format = useFormatter();
  const morning = useMemo(() => panel([{ name: "Terra", color: "var(--color-modis)" }], format), [format]);
  const afternoon = useMemo(
    () =>
      panel(
        [
          { name: "Aqua", color: "var(--color-ember)" },
          { name: "Suomi-NPP", color: "var(--color-mute)" },
        ],
        format,
      ),
    [format],
  );

  return (
    <figure className="grid gap-3">
      <div className="grid gap-6 sm:grid-cols-2">
        <div className="grid gap-1">
          <span className="font-data text-xs text-ink">{t("terraTitle")}</span>
          <PlotFigure build={morning} height={150} label={t("terraLabel")} />
        </div>
        <div className="grid gap-1">
          <span className="font-data text-xs text-ink">{t("aquaTitle")}</span>
          <PlotFigure build={afternoon} height={150} label={t("aquaLabel")} />
        </div>
      </div>
      <figcaption className="font-data text-xs text-mute">{t("overpassCaption")}</figcaption>
    </figure>
  );
}
