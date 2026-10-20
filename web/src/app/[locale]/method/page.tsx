import type { Metadata } from "next";
import { getFormatter, getTranslations } from "next-intl/server";
import { OverpassDrift } from "@/components/charts/OverpassDrift";
import { scoreboard, truth, type Formatter, type Method, type Metrics } from "@/lib/data";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("Meta");
  return { title: t("method") };
}

type Row = {
  key: "cellR" | "cellRmse" | "cellBias" | "countryErr" | "biomeErr";
  scale: keyof typeof scoreboard.scales;
  metric: keyof Metrics;
  best: "max" | "min" | "absmin";
  fmt: (f: Formatter, v: number) => string;
};

const fixed = (digits: number) => (f: Formatter, v: number) =>
  f.number(v, { minimumFractionDigits: digits, maximumFractionDigits: digits });
const percent = (signed: boolean) => (f: Formatter, v: number) =>
  f.number(v / 100, { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: signed ? "exceptZero" : "auto" });

const rows: Row[] = [
  { key: "cellR", scale: "cell_month", metric: "r", best: "max", fmt: fixed(3) },
  { key: "cellRmse", scale: "cell_month", metric: "rmse", best: "min", fmt: fixed(1) },
  { key: "cellBias", scale: "cell_month", metric: "bias_pct", best: "absmin", fmt: percent(true) },
  { key: "countryErr", scale: "country_iso3_year", metric: "median_abs_pct_err", best: "min", fmt: percent(false) },
  { key: "biomeErr", scale: "biome_name_year", metric: "median_abs_pct_err", best: "min", fmt: percent(false) },
];

function bestMethod(row: Row): Method {
  // Raw MODIS is the reference to beat, not a candidate
  const candidates = scoreboard.methods.filter((m) => m !== "raw_modis");
  const score = (m: Method) => {
    const v = scoreboard.scales[row.scale][m][row.metric];
    return row.best === "max" ? -v : row.best === "absmin" ? Math.abs(v) : v;
  };
  return candidates.reduce((a, b) => (score(b) < score(a) ? b : a));
}

export default async function MethodPage() {
  const t = await getTranslations("Method");
  const format = await getFormatter();
  const [first, last] = scoreboard.test_years.map(String);

  return (
    <main className="mx-auto grid max-w-[1100px] gap-12 px-4 py-12 sm:px-6">
      <header className="grid max-w-[66ch] gap-3">
        <span className="font-data text-[11px] tracking-[.1em] text-ember uppercase">{t("eyebrow")}</span>
        <h1 className="font-voice text-[clamp(28px,4vw,40px)] leading-tight text-balance">{t("title")}</h1>
        <p className="text-mute">{t("intro", { first, last })}</p>
      </header>

      <section aria-labelledby="scoreboard" className="grid gap-3">
        <h2 id="scoreboard" className="sr-only">
          {t("scoreboard")}
        </h2>
        <div className="overflow-x-auto">
          <table className="w-full min-w-[560px] border-collapse font-data text-[13px] tabular-nums">
            <thead>
              <tr className="text-[11px] tracking-wide text-mute uppercase">
                <th className="border-b border-line px-2.5 py-2 text-left font-medium">{t("measure")}</th>
                {scoreboard.methods.map((m) => (
                  <th key={m} className="border-b border-line px-2.5 py-2 text-right font-medium">
                    {t(`methods.${m}`)}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => {
                const best = bestMethod(row);
                return (
                  <tr key={row.key}>
                    <td className="border-b border-line px-2.5 py-2.5 font-ui">{t(`rows.${row.key}`)}</td>
                    {scoreboard.methods.map((m) => (
                      <td
                        key={m}
                        className={`border-b border-line px-2.5 py-2.5 text-right ${
                          m === best ? "text-flare" : m === "raw_modis" ? "text-mute" : ""
                        }`}
                      >
                        {row.fmt(format, scoreboard.scales[row.scale][m][row.metric])}
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
        <p className="font-data text-xs text-mute">{t("footnote", { n: format.number(scoreboard.scales.cell_month.n) })}</p>
      </section>

      <section aria-labelledby="truth" className="grid gap-3">
        <h2 id="truth" className="font-data text-[11px] tracking-[.1em] text-mute uppercase">
          {t("truthTitle")}
        </h2>
        <div className="grid gap-px border border-line bg-line sm:grid-cols-3">
          {truth.map((s) => (
            <div key={s.key} className="grid gap-0.5 bg-orbit px-4 py-3">
              <b className="font-data text-xl font-medium">{format.number(s.cell_years)}</b>
              <span className="text-xs text-mute">
                {t("truthCell", {
                  name: s.name,
                  country: t(`countries.${s.key as "nbac" | "mtbs" | "mapbiomas"}`),
                  first: String(s.first),
                  last: String(s.last),
                })}
              </span>
            </div>
          ))}
        </div>
      </section>

      <section aria-labelledby="drift" className="grid gap-3">
        <h2 id="drift" className="font-data text-[11px] tracking-[.1em] text-mute uppercase">
          {t("driftTitle")}
        </h2>
        <OverpassDrift />
      </section>
    </main>
  );
}
