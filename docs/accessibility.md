# Accessibility self-assessment — WCAG 2.1 AA (S15-06)

**Status: self-assessed, 29 Jul 2026.** This is an engineering self-assessment
against WCAG 2.1 AA using automated scanning and keyboard drills. It is NOT a
conformance claim: a defensible compliance statement requires manual testing
with assistive technologies (screen readers, switch access, magnification) and
expert review, which are explicitly out of scope at Alpha 4 (FSD §7 S15-06).

## How this was assessed

1. **Automated scan** — axe-core (`wcag2a` + `wcag2aa` rulesets) wired into the
   live smoke probe (`scripts/ui-smoke.mjs`, section 5). Scans `/projects`,
   `/chat`, and `/docs` after a real hosted-UI sign-in; any serious/critical
   violation fails the smoke. Dependencies live in the repo-root
   `package.json`, so the scan can no longer silently skip.
2. **Keyboard-path drill** — chat (new session → describe → generate), spec
   editor (tab between docs, edit, save, comments), deploy console
   (deploy/extend/teardown) driven by keyboard only, checked at code level.
3. **Contrast pass** — dark-theme text colors raised to AA ratios
   (`globals.css` S15-06 notes: slate-500 → slate-400 on dark surfaces,
   indigo-600 buttons, `#94a3b8` minimum body text).

## What is in place

| Area | Provision |
|------|-----------|
| Focus visibility | Global `:focus-visible` outline (2px indigo, offset) — never removed |
| Skip navigation | "Skip to main content" link, first tab stop in the app shell |
| Modals | Shared `ModalShell`: `role="dialog"`, `aria-modal`, labelled, Escape closes, focus moved in on open / restored on close, Tab trapped (chat new-session, deployment extend, confirm dialogs) |
| Menus | Profile menu: `aria-haspopup`/`aria-expanded`, `role="menu"`/`menuitem`, Escape closes (keyboard equivalent of mouseleave) |
| Chat | Composer textarea has an accessible name; Enter/Shift+Enter documented in placeholder; message thread is `role="log"`; errors render as visible text with a troubleshooting link |
| Icon-only controls | `aria-label` on the notification bell, comment toggle, payload selector, account menu |
| Docs & help | In-app docs navigable by keyboard; `aria-current` marks the active page; help affordances are real links, not hover tooltips |
| Images | Logo images are decorative (`alt=""`); status conveyed as text pills, not color alone |
| Errors | Governance refusals (403/409/422/429) surface as plain-language text with a next step (S15-05), not raw JSON |

## Known gaps (recorded, with owners)

| Gap | Impact | Owner / plan |
|-----|--------|--------------|
| Monaco editor (spec edit mode) has its own keyboard model; Tab inserts spaces rather than moving focus (standard editor behavior; Esc then Tab exits) | Keyboard users need the escape hatch documented | docs/troubleshooting; revisit if user reports |
| Mermaid diagrams render as SVG without long descriptions | Screen-reader users get the diagram title only | Backlog — needs authored descriptions per diagram |
| Live deploy event stream is not an `aria-live` region (deliberate: high-frequency updates would be noisy); final status IS reflected in the labelled status pill | Screen-reader users poll the status pill | Accepted for Alpha 4 |
| Toast notifications (sonner) rely on the library's built-in announcements | Unverified against real AT | Verify during manual AT pass (pre-Preview) |

## Path to a compliance claim

Before any external conformance statement (Preview gate): manual NVDA + VoiceOver
walkthroughs of the five core flows, switch-access spot check, 200% zoom /
reflow verification, and an expert review of the findings above.
