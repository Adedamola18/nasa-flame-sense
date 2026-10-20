"use client";

import * as Plot from "@observablehq/plot";
import { useEffect, useRef } from "react";

/** Renders an Observable Plot chart at the container's width, redrawing on resize. */
export function PlotFigure({
  build,
  height,
  label,
}: {
  build: (width: number) => Plot.PlotOptions;
  height: number;
  label: string;
}) {
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const draw = () => {
      const chart = Plot.plot({
        height,
        style: { background: "transparent", color: "var(--color-mute)", fontFamily: "var(--font-data)", fontSize: "10px" },
        ...build(el.clientWidth),
        width: el.clientWidth,
      });
      chart.setAttribute("aria-label", label);
      el.replaceChildren(chart);
    };
    draw();
    const ro = new ResizeObserver(draw);
    ro.observe(el);
    return () => ro.disconnect();
  }, [build, height, label]);

  return <div ref={ref} role="img" aria-label={label} style={{ minHeight: height }} className="w-full" />;
}
