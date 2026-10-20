import createMiddleware from "next-intl/middleware";
import { routing } from "./i18n/routing";

// Serves English at / and French at /fr; redirects /en/... to the unprefixed path
export default createMiddleware(routing);

export const config = {
  // Everything except API routes, Next internals and files with an extension (/data/*.json, the MapLibre worker)
  matcher: "/((?!api|_next|.*\\..*).*)",
};
