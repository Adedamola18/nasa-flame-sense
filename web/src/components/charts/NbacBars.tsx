"use client";

import * as Plot from "@observablehq/plot";
import { useFormatter, useTranslations } from "next-intl";
import { useMemo } from "react";
import { mha, nbac } from "@/lib/data";
import { PlotFigure } from "./PlotFigure";

const first = nbac[0].year;
const last = nbac.at(-1)!.year;

export function NbacBars({ highlight = 2023 }: { highlight?: number }) {
  const t = useTranslations("Charts");
  const format = useFormatter();
  const fmt = (ha: number, digits: number) =>
    format.number(mha(ha), { minimumFractionDigits: digits, maximumFractionDigits: digits });
  const peak = nbac.find((d) => d.year === highlight)!;

  const build = useMemo(() => {
    const tip = (d: (typeof nbac)[number]) =>
      t("nbacTip", {
        year: String(d.year),
        value: format.number(mha(d.ha), { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
      });
    return (): Plot.PlotOptions => ({
      marginLeft: 26,
      marginBottom: 22,
      x: { type: "band", label: null, tickSize: 0, ticks: [first, 2012, last], tickFormat: (d) => String(d) },
      y: { label: "Mha", grid: true, ticks: 4, tickFormat: (d) => format.number(d) },
      marks: [
        Plot.barY(nbac, {
          x: "year",
          y: (d) => mha(d.ha),
          fill: (d) => (d.year === highlight ? "var(--color-ember)" : "var(--color-bar)"),
          inset: 1,
          title: tip,
        }),
        Plot.ruleY([0], { stroke: "var(--color-line)" }),
      ],
    });
  }, [format, highlight, t]);

  return (
    <figure className="grid gap-2">
      <PlotFigure
        build={build}
        height={150}
        label={t("nbacLabel", { first: String(first), last: String(last), peakYear: String(highlight), peak: fmt(peak.ha, 1) })}
      />
      <figcaption className="font-data text-xs leading-relaxed text-mute">
        {t.rich("nbacCaption", {
          first: String(first),
          last: String(last),
          b: (chunks) => <b className="font-medium text-ink">{chunks}</b>,
        })}
      </figcaption>
    </figure>
  );
}
