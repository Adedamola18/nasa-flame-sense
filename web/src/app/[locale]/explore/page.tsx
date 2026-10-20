import type { Metadata } from "next";
import { getTranslations } from "next-intl/server";
import { ExplorerPanels } from "@/components/explore/ExplorerPanels";

export async function generateMetadata(): Promise<Metadata> {
  const t = await getTranslations("Meta");
  return { title: t("explorer") };
}

export default function ExplorePage() {
  return (
    <main>
      <ExplorerPanels />
    </main>
  );
}
