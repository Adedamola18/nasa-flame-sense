import {
  canadaSeason,
  longDate,
  mha,
  monthName,
  overpass,
  overpassAt,
  scoreboard,
  solarTime,
  storyFigures as sf,
  type Formatter,
} from "./data";

export type Camera = { center: [number, number]; zoom: number };

/** Message values for a chapter's narration, formatted for the reader's locale. */
type Values = (format: Formatter) => Record<string, string | number>;

export type Chapter = {
  n: number;
  /** Key under Story.ch in messages/*.json: title, narrator and (once written) text */
  key: `c${number}`;
  camera: Camera;
  /** Placeholders the chapter's text can use, e.g. {fireHa}. Shown under unwritten chapters for the writers. */
  values?: Values;
  figure?: "nbac" | "overpass";
};

const s2023 = canadaSeason(2023);
const rawGap = Math.abs(scoreboard.scales.cell_month.raw_modis.bias_pct) / 100;
// Last year every satellite has, so the drift is compared on the same footing
const lastOverpassYear = Math.min(...Object.values(overpass).map((s) => s.at(-1)!.year));

const int = (f: Formatter, n: number) => f.number(n, { maximumFractionDigits: 0 });
const pct = (f: Formatter, share: number) => f.number(share, { style: "percent", maximumFractionDigits: 0 });

/** Figures for a featured fire (chapters 4 and 6): NBAC area and dates, S-NPP detections while it burned. */
const fireValues = (fire: typeof sf.c4) => (f: Formatter) => ({
  fireHa: int(f, fire.fireHa),
  agencyStart: longDate(f, fire.agencyStart),
  hotspotEnd: longDate(f, fire.hotspotEnd),
  peakDay: longDate(f, fire.peakDay),
  peakDayDetections: int(f, fire.peakDayDetections),
  detections: int(f, fire.detections),
});

export const chapters: Chapter[] = [
  {
    n: 1,
    key: "c1",
    camera: { center: [-30, 20], zoom: 1.1 },
    values: (f) => ({
      year: String(sf.c1.year),
      detections: int(f, sf.c1.detections),
      perDay: int(f, sf.c1.perDay),
      countries: sf.c1.countries,
      peakMonth: monthName(f, sf.c1.peakMonth),
      peakShare: pct(f, sf.c1.peakShare),
    }),
  },
  {
    n: 2,
    key: "c2",
    camera: { center: [20, 5], zoom: 1.1 },
    values: (f) => ({
      year: String(sf.c2.year),
      terraDay: int(f, sf.c2.terraDay),
      aquaDay: int(f, sf.c2.aquaDay),
      aquaToTerra: f.number(sf.c2.aquaToTerra, { maximumFractionDigits: 1 }),
      terraTime: solarTime(f, overpassAt("Terra", sf.c2.year)),
      aquaTime: solarTime(f, overpassAt("Aqua", sf.c2.year)),
    }),
  },
  {
    n: 3,
    key: "c3",
    camera: { center: [-100, 50], zoom: 1.6 },
    values: (f) => ({ rawGap: pct(f, rawGap) }),
  },
  { n: 4, key: "c4", camera: { center: [-111.4, 56.7], zoom: 4.5 }, values: fireValues(sf.c4) },
  {
    n: 5,
    key: "c5",
    camera: { center: [-100, 58], zoom: 2.3 },
    figure: "nbac",
    values: (f) => ({
      year: "2023",
      burned: f.number(mha(s2023.ha), { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
      ratio: f.number(s2023.ratio, { maximumFractionDigits: 0 }),
      firstYear: String(s2023.firstYear),
      seasons: s2023.seasonsOutweighed,
    }),
  },
  { n: 6, key: "c6", camera: { center: [-118.1, 52.9], zoom: 5 }, values: fireValues(sf.c6) },
  {
    n: 7,
    key: "c7",
    camera: { center: [60, -5], zoom: 1.1 },
    values: (f) => ({
      blackSummerRatio: f.number(sf.c7.aus.ratio, { maximumFractionDigits: 0 }),
      blackSummerDetections: int(f, sf.c7.aus.detections),
      amazonPeakMonth: monthName(f, sf.c7.amazon.peakMonth),
      amazonAugOctShare: pct(f, sf.c7.amazon.augOctShare),
      savannaNorthPeak: monthName(f, sf.c7.africa.northPeakMonth),
      savannaSouthPeak: monthName(f, sf.c7.africa.southPeakMonth),
    }),
  },
  {
    n: 8,
    key: "c8",
    camera: { center: [0, 15], zoom: 1 },
    figure: "overpass",
    values: (f) => ({
      terraFrom: solarTime(f, overpassAt("Terra", 2003)),
      terraTo: solarTime(f, overpassAt("Terra", lastOverpassYear)),
      aquaFrom: solarTime(f, overpassAt("Aqua", 2012)),
      aquaTo: solarTime(f, overpassAt("Aqua", lastOverpassYear)),
      lastYear: String(lastOverpassYear),
    }),
  },
  // Chapter 9 waits for the merged 2001–2024 record; chapter 10 introduces the Explorer
  { n: 9, key: "c9", camera: { center: [-100, 50], zoom: 1.4 } },
  { n: 10, key: "c10", camera: { center: [-119.5, 49.9], zoom: 4 } },
];
