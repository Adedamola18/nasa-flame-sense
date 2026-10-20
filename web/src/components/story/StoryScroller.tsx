"use client";

import dynamic from "next/dynamic";
import { useFormatter, useLocale, useTranslations } from "next-intl";
import { Children, useEffect, useMemo, useState, type ReactNode } from "react";
import { NbacBars } from "@/components/charts/NbacBars";
import { OverpassDrift } from "@/components/charts/OverpassDrift";
import { ListenToggle } from "@/components/ui/buttons";
import { Link } from "@/i18n/navigation";
import { chapters, type Chapter } from "@/lib/chapters";

// MapLibre needs the browser (WebGL), so the globe is never prerendered.
const Globe = dynamic(() => import("@/components/map/Globe"), { ssr: false });

const SPEECH_LANG = { en: "en-CA", fr: "fr-CA" } as const;

const SHORT_FIGURE = 16; // characters: "375 m", "14.7 Mha", "16 May 2016" stay on one line

/**
 * A data figure inside narration. Short figures keep their parts together with non-breaking spaces;
 * longer ones wrap like text, so a figure can never push past the column.
 * translate="no" stops browser translation from rewording numbers or restructuring the paragraph.
 */
function Figure({ children }: { children: ReactNode }) {
  const parts = Children.toArray(children);
  const text = parts.every((p) => typeof p === "string" || typeof p === "number") ? parts.join("") : null;
  return (
    <span translate="no" className="font-data text-[0.78em] font-medium text-flare not-italic">
      {text !== null && text.length <= SHORT_FIGURE ? text.replace(/ /g, "\u00a0") : children}
    </span>
  );
}

/** Narration for one chapter: styled for reading and plain for speech. null when not written yet. */
function useNarration(chapter: Chapter) {
  const t = useTranslations("Story.ch");
  const format = useFormatter();
  return useMemo(() => {
    const key = `${chapter.key}.text`;
    if (!t.has(key)) return null;
    const values = chapter.values?.(format) ?? {};
    return {
      rich: t.rich(key, {
        ...values,
        fig: (chunks) => <Figure>{chunks}</Figure>,
      }),
      plain: t.markup(key, { ...values, fig: (chunks) => chunks }),
    };
  }, [chapter, t, format]);
}

/*
 * Writers' cheat sheet, switched off: listed each unwritten chapter's placeholders and their values.
 * To bring it back, uncomment this and the <WriterFigures /> line in ChapterSection.
 *
 * // For unwritten chapters: the placeholders its text can use, with their current values.
 * function WriterFigures({ chapter }: { chapter: Chapter }) {
 *   const t = useTranslations("Story");
 *   const format = useFormatter();
 *   const values = chapter.values?.(format);
 *   if (!values) return null;
 *   return (
 *     <div className="grid gap-2 border border-dashed border-line px-3 py-2.5">
 *       <span className="font-data text-[11px] text-mute">{t("writerFigures")}</span>
 *       <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 font-data text-xs">
 *         {Object.entries(values).map(([k, v]) => (
 *           <div key={k} className="contents">
 *             <dt className="text-mute">{`{${k}}`}</dt>
 *             <dd className="text-flare">{v}</dd>
 *           </div>
 *         ))}
 *       </dl>
 *     </div>
 *   );
 * }
 */

function ChapterSection({ chapter, active }: { chapter: Chapter; active: boolean }) {
  const t = useTranslations("Story");
  const narration = useNarration(chapter);
  return (
    <section
      id={`chapter-${chapter.n}`}
      data-step
      className={`flex min-h-[calc(55dvh-3.5rem)] scroll-mt-[calc(45dvh+3.5rem)] flex-col justify-start gap-5 bg-night px-5 py-10 transition-opacity sm:px-7 lg:min-h-[85dvh] lg:scroll-mt-14 lg:justify-center lg:py-16 ${
        active ? "opacity-100" : "opacity-40"
      }`}
    >
      <span className="font-data text-[11px] tracking-[.1em] text-mute uppercase">
        {t("chapterLabel", { n: chapter.n })} · <span className="text-ember">{t(`ch.${chapter.key}.title`)}</span>
      </span>
      <span className="font-data text-[11px] text-mute">{t("narratedBy", { narrator: t(`ch.${chapter.key}.narrator`) })}</span>
      {narration ? (
        <p className="max-w-[34ch] font-voice text-[21px] leading-[1.45] italic">{narration.rich}</p>
      ) : (
        <>
          <p className="justify-self-start border border-dashed border-line px-2 py-1 font-data text-[11px] text-mute">{t("notWritten")}</p>
          {/* <WriterFigures chapter={chapter} /> */}
        </>
      )}
      {chapter.figure === "nbac" && <NbacBars />}
      {chapter.figure === "overpass" && <OverpassDrift />}
      {chapter.n === 10 && (
        <Link href="/explore" className="font-medium text-ember hover:text-ember-hover">
          {t("toExplorer")}
        </Link>
      )}
    </section>
  );
}

export function StoryScroller() {
  const t = useTranslations("Story");
  const tMap = useTranslations("Map");
  const locale = useLocale();
  const [active, setActive] = useState(0);
  const [speaking, setSpeaking] = useState(false);
  const chapter = chapters[active];
  const narration = useNarration(chapter);

  useEffect(() => {
    let scroller: { destroy: () => void } | undefined;
    let cancelled = false;
    import("scrollama").then(({ default: scrollama }) => {
      if (cancelled) return;
      const s = scrollama();
      // Phones: the globe covers the top 45%, so the reading line sits lower
      const offset = window.matchMedia("(min-width: 1024px)").matches ? 0.6 : 0.7;
      s.setup({ step: "[data-step]", offset }).onStepEnter(({ index }) => setActive(index));
      scroller = s;
    });
    return () => {
      cancelled = true;
      scroller?.destroy();
    };
  }, []);

  // Narration voice: browser speech for now; Workers AI TTS later.
  useEffect(() => {
    if (!speaking || typeof speechSynthesis === "undefined") return;
    speechSynthesis.cancel();
    if (!narration) return;
    const u = new SpeechSynthesisUtterance(narration.plain);
    u.lang = SPEECH_LANG[locale];
    speechSynthesis.speak(u);
    return () => speechSynthesis.cancel();
  }, [speaking, narration, locale]);

  return (
    <div className="grid lg:grid-cols-[minmax(0,420px)_minmax(0,1fr)]">
      {/* Globe: sticky beside the text on desktop; on phones it is pinned above and the text scrolls under it */}
      <div className="sticky top-14 z-20 h-[45dvh] border-b border-line bg-night lg:order-last lg:z-0 lg:h-[calc(100dvh-3.5rem)] lg:border-b-0 lg:border-l">
        <Globe camera={chapter.camera} />
        <div className="pointer-events-none absolute top-3 left-3 grid gap-1 bg-night/80 px-2 py-1.5 font-data text-[11px] text-mute">
          <span className="flex items-center gap-2">
            <span className="h-1.5 w-12 bg-gradient-to-r from-char via-ember to-flare sm:w-[90px]" />
            <span className="text-ink">{t("legendTitle", { year: "2023" })}</span>
          </span>
          <span>{t("legendSource")}</span>
          <span className="hidden sm:inline">{tMap("hint")}</span>
        </div>
        <div className="absolute top-3 right-3">
          <ListenToggle playing={speaking} onToggle={() => setSpeaking((v) => !v)} />
        </div>
        <ol aria-label={t("chapters")} className="absolute inset-x-4 bottom-4 grid grid-cols-10 gap-1">
          {chapters.map((c, i) => (
            <li key={c.n}>
              <a
                href={`#chapter-${c.n}`}
                aria-current={i === active ? "step" : undefined}
                title={t("chapterLink", { n: c.n, title: t(`ch.${c.key}.title`) })}
                className="block py-2"
              >
                <span className={`block h-[3px] ${i === active ? "bg-ember" : i < active ? "bg-[#5a6676]" : "bg-line"}`} />
                <span className="sr-only">{t("chapterLink", { n: c.n, title: t(`ch.${c.key}.title`) })}</span>
              </a>
            </li>
          ))}
        </ol>
      </div>

      <div className="relative z-10">
        {chapters.map((c, i) => (
          <ChapterSection key={c.n} chapter={c} active={i === active} />
        ))}
      </div>
    </div>
  );
}
