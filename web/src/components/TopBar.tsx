"use client";

import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { getPathname, Link, usePathname } from "@/i18n/navigation";
import { routing, type Locale } from "@/i18n/routing";
import { Segmented } from "./ui/buttons";

const sections = [
  { href: "/", key: "story" },
  { href: "/explore", key: "explorer" },
  { href: "/method", key: "method" },
] as const;

export function TopBar() {
  const t = useTranslations("Nav");
  const locale = useLocale();
  const pathname = usePathname(); // without the locale prefix
  const router = useRouter();

  // Keep the reader's place (the #chapter anchor) when switching language.
  // getPathname applies the prefix rule: /explore for English, /fr/explore for French.
  const switchTo = (next: Locale) =>
    router.replace(`${getPathname({ href: pathname, locale: next })}${window.location.hash}`, { scroll: false });

  return (
    <header className="sticky top-0 z-30 flex h-14 items-center gap-3 border-b border-line bg-night/95 px-3 backdrop-blur sm:gap-6 sm:px-5">
      {/* Logo is a brand name, the same in every language; page titles still use Meta.title */}
      <Link href="/" className="font-voice text-[17px] whitespace-nowrap italic sm:text-[19px]">
        Bluck
      </Link>
      <nav aria-label={t("sections")} className="flex gap-3 text-[13px] sm:gap-5">
        {sections.map((s) => {
          const active = s.href === "/" ? pathname === "/" : pathname.startsWith(s.href);
          return (
            <Link
              key={s.href}
              href={s.href}
              aria-current={active ? "page" : undefined}
              className={`border-b py-1 ${active ? "border-ember text-ink" : "border-transparent text-mute hover:text-ink"}`}
            >
              {t(s.key)}
            </Link>
          );
        })}
      </nav>
      <div className="ml-auto">
        <Segmented<Locale>
          label={t("language")}
          value={locale}
          onChange={switchTo}
          options={routing.locales.map((l) => ({ value: l, label: l.toUpperCase() }))}
        />
      </div>
    </header>
  );
}
