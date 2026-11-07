---
name: frontend-react
roles: architect, developer
keywords: react, tsx, jsx, vite, typescript, frontend, front-end, pwa, spa, component, components, web app, webapp, browser, html, css
---
# React + TypeScript front-end playbook (senior level)

## Structure
- `src/app/` (shell, providers, routing), `src/features/<feature>/` (components, hooks, logic, tests together),
  `src/ui/` (design-system components: Button, Card, Field, Dialog...), `src/lib/` (pure utilities),
  `src/styles/tokens.css` (all design tokens). No file over ~200 lines without a reason.
- TypeScript `strict`; no `any`; typed props; discriminated unions for view state
  (`{status:'loading'} | {status:'error', message} | {status:'ready', data}`); validate data from storage/network.

## Components
- Semantic HTML first: `<button>`, `<a>`, `<label>`, `<main>`, `<nav>`, headings in order. Never a clickable `<div>`.
- Small presentational components + a few containers that own state. Composition (children, slots) over long prop
  lists. Stable keys. Controlled inputs with visible labels and error text.
- Every async view renders loading, empty, error (with retry) and ready states.

## State and effects
- Local state first; lift only when shared; context for app-wide concerns (theme, locale, settings).
- `useReducer` for multi-step flows. Derive values instead of syncing them. Effects only to sync with the outside
  world, always with cleanup. Memoise only what is measured to be slow.

## Storage and data
- A small repository module per store (e.g. IndexedDB with a localStorage fallback) with async functions, a schema
  version and a migration path. Components never touch storage APIs directly.

## Styling
- CSS variables from `tokens.css` everywhere; no hard-coded colours or magic spacing numbers.
- Mobile-first media queries; CSS logical properties (`margin-inline-start`, `padding-inline`, `inset-inline`) so RTL
  works without overrides; `:focus-visible` rings; `prefers-reduced-motion` and `prefers-color-scheme` respected.
- One styling approach per project (CSS modules or one stylesheet per component), applied consistently.

## RTL and i18n
- `lang` and `dir` on `<html>`; isolate embedded LTR text (`<bdi>` or `dir="ltr"` + `lang="en"` on the element);
  strings in locale files, never inline; `Intl` for numbers and dates.

## Performance and PWA
- Lazy-load heavy routes; size images; preload the UI font with `font-display: swap`; keep the JS bundle lean.
- Manifest complete (name, icons 192/512 incl. maskable, theme/background colour, start_url, display). Versioned
  service-worker caches, an update flow the user can see, an offline fallback.

## Tests
- Vitest + Testing Library: query by role and label (`getByRole('button', {name: ...})`), drive with user events,
  assert what the user sees. Playwright for multi-step flows and the 390px / 1280px layouts.
- Done means: no console errors or warnings, every action reachable by keyboard, tests pass.
