/*
 * Typed access to the files written by pipeline/web_seed.py.
 * Story text must take its figures from here, never from literals.
 */
import type { createFormatter } from "next-intl";
import nbacJson from "@/data/nbac_canada.json";
import overpassJson from "@/data/overpass.json";
import scoreboardJson from "@/data/scoreboard.json";
import storyJson from "@/data/story_figures.json";
import truthJson from "@/data/truth_coverage.json";

export type Formatter = ReturnType<typeof createFormatter>;
export type Method = "raw_modis" | "global_multiplier" | "quantile_mapping" | "ml";
export type Metrics = { r: number; rmse: number; bias_pct: number; median_abs_pct_err: number };
export type Scale = { label: string; n: number } & Record<Method, Metrics>;
export type Satellite = "Terra" | "Aqua" | "Suomi-NPP";

export const nbac = nbacJson.years;
export const overpass = overpassJson.series as Record<Satellite, { year: number; hour: number }[]>;
export const scoreboard = scoreboardJson as {
  test_years: [number, number];
  methods: Method[];
  scales: Record<"cell_month" | "country_iso3_year" | "biome_name_year", Scale>;
};
export const truth = truthJson;
export const storyFigures = storyJson;

/** A calendar date such as 16 May 2016 (en) or 16 mai 2016 (fr). Dates are ISO strings in UTC. */
export function longDate(format: Formatter, iso: string) {
  return format.dateTime(new Date(`${iso}T00:00:00Z`), { day: "numeric", month: "long", year: "numeric", timeZone: "UTC" });
}

/** A month name: August, août. */
export function monthName(format: Formatter, month: number) {
  return format.dateTime(new Date(Date.UTC(2000, month - 1, 1)), { month: "long", timeZone: "UTC" });
}

export const mha = (ha: number) => ha / 1e6;

/** Local solar time, e.g. 10:39 in English and 10 h 39 in French. */
export function solarTime(format: Formatter, hour: number) {
  const minutes = Math.round(hour * 60);
  return format.dateTime(new Date(Date.UTC(2000, 0, 1, 0, minutes)), {
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
    timeZone: "UTC",
  });
}

/** Figures for chapter 5, derived from NBAC. */
export function canadaSeason(year: number) {
  const byYear = new Map(nbac.map((d) => [d.year, d.ha]));
  const ha = byYear.get(year) ?? 0;
  const before = nbac.filter((d) => d.year < year);
  const mean = before.reduce((s, d) => s + d.ha, 0) / before.length;
  // How many consecutive seasons before `year` it outweighs, combined
  let seasons = 0;
  let sum = 0;
  for (let y = year - 1; y >= nbac[0].year; y--) {
    if (sum + (byYear.get(y) ?? 0) >= ha) break;
    sum += byYear.get(y) ?? 0;
    seasons++;
  }
  return { ha, ratio: ha / mean, firstYear: nbac[0].year, seasonsOutweighed: seasons };
}

export function overpassAt(sat: Satellite, year: number) {
  return overpass[sat].find((d) => d.year === year)!.hour;
}
