// S14-01: self-host the Monaco editor assets.
//
// @monaco-editor/react loads Monaco from cdn.jsdelivr.net by default. That is a
// third-party script executing in the app at runtime — blocked by our CSP, and
// undesirable regardless: it adds an external availability dependency, leaks
// user requests to a CDN, and pulls a DIFFERENT Monaco version (0.55.1) than
// the one we install and audit (0.56.0).
//
// Runs automatically before `next build` (prebuild), so both local builds and
// the container image get the assets.
import { cp, mkdir, rm, stat } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = join(dirname(fileURLToPath(import.meta.url)), "..");
const source = join(root, "node_modules", "monaco-editor", "min", "vs");
const target = join(root, "public", "monaco", "vs");

try {
  await stat(source);
} catch {
  console.error(`copy-monaco: ${source} not found — is monaco-editor installed?`);
  process.exit(1);
}

await rm(target, { recursive: true, force: true });
await mkdir(dirname(target), { recursive: true });
await cp(source, target, { recursive: true });
console.log(`copy-monaco: vendored Monaco assets -> public/monaco/vs`);
