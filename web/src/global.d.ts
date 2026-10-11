import type { routing } from "@/i18n/routing";

// Narrows next-intl's locale type to the locales we ship
declare module "next-intl" {
  interface AppConfig {
    Locale: (typeof routing.locales)[number];
  }
}
