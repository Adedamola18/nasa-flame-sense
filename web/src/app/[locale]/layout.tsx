import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans, Newsreader } from "next/font/google";
import { notFound } from "next/navigation";
import { hasLocale, NextIntlClientProvider } from "next-intl";
import { getTranslations } from "next-intl/server";
import { TopBar } from "@/components/TopBar";
import { getPathname } from "@/i18n/navigation";
import { routing } from "@/i18n/routing";
import "../globals.css";

const plexSans = IBM_Plex_Sans({
  variable: "--font-plex-sans",
  subsets: ["latin"],
  weight: ["400", "500", "600"],
});

const plexMono = IBM_Plex_Mono({
  variable: "--font-plex-mono",
  subsets: ["latin"],
  weight: ["400", "500"],
});

const newsreader = Newsreader({
  variable: "--font-newsreader",
  subsets: ["latin"],
  style: ["normal", "italic"],
});

export function generateStaticParams() {
  return routing.locales.map((locale) => ({ locale }));
}

export async function generateMetadata({ params }: LayoutProps<"/[locale]">): Promise<Metadata> {
  const { locale } = await params;
  const t = await getTranslations({ locale: hasLocale(routing.locales, locale) ? locale : routing.defaultLocale, namespace: "Meta" });
  return {
    title: t("title"),
    description: t("description"),
    alternates: { languages: Object.fromEntries(routing.locales.map((l) => [l, getPathname({ href: "/", locale: l })])) },
  };
}

export default async function RootLayout({ children, params }: LayoutProps<"/[locale]">) {
  const { locale } = await params;
  if (!hasLocale(routing.locales, locale)) notFound();

  return (
    <html lang={locale} className={`${plexSans.variable} ${plexMono.variable} ${newsreader.variable} antialiased`}>
      <body className="min-h-dvh">
        <NextIntlClientProvider>
          <TopBar />
          {children}
        </NextIntlClientProvider>
      </body>
    </html>
  );
}
