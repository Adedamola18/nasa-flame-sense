"use client";

import { useTranslations } from "next-intl";
import { useState } from "react";
import { Button, Chip } from "@/components/ui/buttons";

const inputBox =
  "flex h-[42px] items-center gap-2.5 border border-line bg-night px-3 focus-within:outline-2 focus-within:outline-offset-2 focus-within:outline-flare";

function Pending({ children }: { children: React.ReactNode }) {
  return <p className="justify-self-start border border-dashed border-line px-2 py-1 font-data text-[11px] text-mute">{children}</p>;
}

export function ExplorerPanels() {
  const t = useTranslations("Explorer");
  const [place, setPlace] = useState("");
  const [question, setQuestion] = useState("");
  const [layers, setLayers] = useState({ harmonised: true, raw: false, nbac: false });
  const toggle = (k: keyof typeof layers) => setLayers((l) => ({ ...l, [k]: !l[k] }));

  return (
    <div className="grid min-h-[calc(100dvh-3.5rem)] lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      <section aria-labelledby="place-h" className="grid content-start gap-5 px-5 py-7 sm:px-7">
        <h1 id="place-h" className="font-data text-[11px] tracking-[.1em] text-mute uppercase">
          {t("placeTitle")}
        </h1>
        <form className={inputBox} role="search" onSubmit={(e) => e.preventDefault()}>
          <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden="true" className="shrink-0 text-mute">
            <circle cx="7" cy="7" r="5" fill="none" stroke="currentColor" strokeWidth="1.5" />
            <path d="M11 11l4 4" stroke="currentColor" strokeWidth="1.5" />
          </svg>
          <input
            id="place"
            value={place}
            onChange={(e) => setPlace(e.target.value)}
            placeholder={t("searchPlaceholder")}
            aria-label={t("searchLabel")}
            className="h-full min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-mute/70"
          />
        </form>
        <div className="flex flex-wrap gap-2">
          <Chip pressed={layers.harmonised} onToggle={() => toggle("harmonised")}>
            {t("layerHarmonised")}
          </Chip>
          <Chip pressed={layers.raw} onToggle={() => toggle("raw")} color="var(--color-modis)">
            {t("layerRaw")}
          </Chip>
          <Chip pressed={layers.nbac} onToggle={() => toggle("nbac")} color="#c9b38a">
            {t("layerNbac")}
          </Chip>
        </div>
        <Pending>{t("placePending")}</Pending>
      </section>

      <section aria-labelledby="ask-h" className="grid content-start gap-4 border-t border-line px-5 py-7 sm:px-7 lg:border-t-0 lg:border-l">
        <h2 id="ask-h" className="font-data text-[11px] tracking-[.1em] text-mute uppercase">
          {t("askTitle")}
        </h2>
        <form className="grid gap-3" onSubmit={(e) => e.preventDefault()}>
          <label className={inputBox} htmlFor="ask">
            <input
              id="ask"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={t("askPlaceholder")}
              aria-label={t("askTitle")}
              className="h-full min-w-0 flex-1 bg-transparent text-[15px] outline-none placeholder:text-mute/70"
            />
          </label>
          <div className="flex flex-wrap gap-2.5">
            <Button type="submit" variant="primary" disabled>
              {t("ask")}
            </Button>
            <Button disabled>{t("compare")}</Button>
          </div>
        </form>
        <Pending>{t("askPending")}</Pending>
      </section>
    </div>
  );
}
