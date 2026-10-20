import { hasLocale } from "next-intl";
import { getRequestConfig } from "next-intl/server";
import { notFound } from "next/navigation";
import * as rootParams from "next/root-params";
import en from "../../messages/en.json";
import { routing } from "./routing";

type Messages = { [key: string]: string | Messages };

/** Fills keys missing from a translation with the English text. */
function withFallback(base: Messages, override: Messages): Messages {
  const out: Messages = { ...base };
  for (const [k, v] of Object.entries(override)) {
    const b = base[k];
    out[k] = typeof v === "object" && typeof b === "object" ? withFallback(b, v) : v;
  }
  return out;
}

export default getRequestConfig(async ({ locale }) => {
  if (!locale) {
    const param = await rootParams.locale();
    if (!hasLocale(routing.locales, param)) notFound();
    locale = param;
  }

  const messages =
    locale === routing.defaultLocale ? en : withFallback(en, (await import(`../../messages/${locale}.json`)).default);

  // Solar times are computed in UTC hours, so format them in UTC
  return { locale, messages, timeZone: "UTC" };
});
