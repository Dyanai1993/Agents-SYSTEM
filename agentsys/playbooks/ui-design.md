---
name: ui-design
roles: architect, developer
keywords: ui, ux, design, page, screen, screens, layout, theme, visual, button, buttons, dashboard, pwa, frontend, front-end, css, html, app
---
# UI design playbook (senior product designer level)

## Tokens first (one file, used everywhere)
- Colour: a neutral scale (50-900), ONE accent, semantic colours (success, warning, danger, info) and semantic roles
  (`--surface`, `--surface-raised`, `--text`, `--text-muted`, `--border`, `--accent`, `--on-accent`). Dark theme and
  high contrast remap the roles, not the components.
- Type scale (for example 14 / 16 / 18 / 20 / 24 / 30 / 36 px) with line-heights (1.5 body, 1.2-1.3 headings);
  spacing on a 4/8 px grid; radii (4 / 8 / 12 / 999); 2-3 shadow levels; z-index layers.

## Typography
- One UI family per script, loaded locally for offline: Hebrew Assistant / Heebo / Rubik, Latin Inter. Two weights
  (400/600) are usually enough. Body 16px minimum on mobile; line length 45-75 characters.

## Layout and hierarchy
- A content column with a max width; gutters 16px on phones, 24-32px on desktop; consistent vertical rhythm from the
  spacing tokens. Whitespace separates groups; cards group related content.
- One primary action per screen, visually obvious; rank everything else with size, weight and colour.
- A real app frame: header with the app name and its key actions; navigation that fits the number of sections.

## Components (each with all states)
- Buttons: primary / secondary / ghost / destructive; at least 44x44px; hover, active, focus-visible, disabled,
  loading. Inputs: visible label, help text, error text, focus ring. Cards: 16-24px padding, radius, a subtle border
  or shadow. Feedback: inline messages, toasts or banners - never silent.
- Empty states explain what to do next and offer the action; loading uses skeletons or progress; errors say what
  happened and offer a retry.

## Colour and accessibility
- Text contrast 4.5:1 (large text 3:1), UI borders and icons 3:1; never meaning by colour alone; accent used
  sparingly on calm neutral surfaces. Visible focus on every interactive element.

## Motion
- 150-250ms ease-out for state changes; nothing that blocks input; honour `prefers-reduced-motion`.

## Tone
- Calm and adult: no clutter, no cartoon styling, real content instead of lorem ipsum, consistent iconography.

## RTL
- Mirror the layout with logical properties; flip directional icons (arrows, chevrons); keep English, numbers and
  code LTR inside RTL text.

## Self-check before you say done
Picture the screen at 390px and at 1280px, in light and dark: is the primary action obvious? Is spacing consistent?
Is any element still in browser-default styling? Does every interactive element show focus? Would a designer ship it?
