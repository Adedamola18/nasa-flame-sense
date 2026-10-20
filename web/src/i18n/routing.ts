import { defineRouting } from "next-intl/routing";

// English and French: Canada's two official languages
export const routing = defineRouting({
  locales: ["en", "fr"],
  defaultLocale: "en",
  // English lives at / (no /en); French at /fr
  localePrefix: "as-needed",
  // / is always English; French is chosen with the toggle or a /fr link, never from the browser language
  localeDetection: false,
});

export type Locale = (typeof routing.locales)[number];
