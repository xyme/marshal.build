import { loader } from "@monaco-editor/react";

/**
 * Point the Monaco loader at our self-hosted copy (S14-01).
 *
 * Import this module once before rendering any editor. Without it the library
 * fetches Monaco from cdn.jsdelivr.net, which the CSP blocks — and which we do
 * not want in any case (third-party runtime script, version drift from the
 * copy we audit). Assets are vendored into public/monaco/vs by the prebuild
 * step (frontend/scripts/copy-monaco.mjs).
 */
loader.config({ paths: { vs: "/monaco/vs" } });

export {};
