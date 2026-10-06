import admin from "@/content/docs/admin.md";
import chatAndSpecs from "@/content/docs/chat-and-specs.md";
import codegen from "@/content/docs/codegen.md";
import deployment from "@/content/docs/deployment.md";
import governance from "@/content/docs/governance.md";
import marketplace from "@/content/docs/marketplace.md";
import personas from "@/content/docs/personas.md";
import quickstart from "@/content/docs/quickstart.md";
import studio from "@/content/docs/studio.md";
import teams from "@/content/docs/teams.md";
import troubleshooting from "@/content/docs/troubleshooting.md";

/**
 * Documentation registry (S15-04). Markdown is imported as a string at build
 * time (see next.config.ts webpack rule), so pages are static and the docs
 * cannot drift from the release they ship with.
 *
 * ANCHORS: `helpHref` values elsewhere in the app point at these slugs, so
 * renaming a slug means updating those links — keep the list here as the
 * single source of truth.
 */
export interface DocPage {
  slug: string;
  title: string;
  content: string;
}

export const DOC_PAGES: DocPage[] = [
  { slug: "quickstart", title: "Quickstart", content: quickstart },
  { slug: "personas", title: "Business & Power", content: personas },
  // Function guides (docs expansion, 5 Aug 2026) — Build · Govern · Deploy order
  { slug: "chat-and-specs", title: "Chat & specifications", content: chatAndSpecs },
  { slug: "studio", title: "The Studio", content: studio }, // S18 surface
  { slug: "codegen", title: "Code generation", content: codegen },
  { slug: "governance", title: "Governance", content: governance },
  { slug: "deployment", title: "Deployment & Enclaves", content: deployment },
  { slug: "marketplace", title: "Templates & Marketplace", content: marketplace },
  { slug: "teams", title: "Teams & sharing", content: teams },
  { slug: "troubleshooting", title: "Troubleshooting", content: troubleshooting },
  { slug: "admin", title: "Administration", content: admin },
  // Release notes (S16-06) were withdrawn from the user-facing registry on
  // 4 Sep 2026 (product decision): src/content/docs/release-notes.md stays in
  // the repository as the internal digest of the §13 build log.
];

export function getDoc(slug: string): DocPage | undefined {
  return DOC_PAGES.find((page) => page.slug === slug);
}
