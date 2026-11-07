---
name: ui-design
roles: architect, developer, tester
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

## Procedure for a screen (every role, before calling it done)
1. Who does this screen serve, and what do they need to understand in the first five seconds?
2. What is the ONE first useful action? Make it the most prominent element. Everything else is secondary - settings,
   rarely used controls and technical status belong in a secondary area (a settings section, a menu, a footer).
3. What does the user see in every state: first visit, in progress (with progress and a way to wait or cancel),
   success, failure (what happened, in plain words) and recovery (the one action that fixes it)?
4. Where does keyboard focus go after each action? A control that disappears must hand focus to its replacement
   (the progress region, the result message, the retry button) - never to the page body.
5. Evidence: look at the real rendered result at the agreed widths and themes (a screenshot you actually opened, or a
   check of computed layout), then write one or two sentences judging it against points 1-4. "Renders" is not
   evidence that it is usable.

Seen in the FirstWords benchmark (T-0026): the first screen listed features (sample tiles, download, update, three
theme buttons) with equal weight and no guidance, so a first-time adult beginner did not know what to do first; and
activating Download removed the focused button, leaving focus on the page body.

## For the Tester
Measure the first screen in a real browser (computed styles and layout, not source text), at 390px and 1280px:
- One first action: exactly one visible control in the accent colour and bold weight, inside the first viewport.
- Guidance: its section has a heading and a sentence (not the status line) that says what the action does.
- Hierarchy: no other control in that section; settings and rarely used controls sit in their own titled section.
- Not browser defaults: a non-default font, a primary control at least 44px tall with rounded corners, a content
  column narrower than the window, colours from custom properties.
- Feedback: a live region (role=status or aria-live) next to the first action, and focus that never falls to the
  body after an action.
- Narrow screens: no horizontal scroll at 390px. Direction: `dir=rtl` for Hebrew, English (`lang=en`) laid out LTR.
Quote the measured values in `evidence`. Whether the screen looks polished is a human judgement: report it as
`unverified` with `not_verified_because: "visual"` and name the screenshots you took. A bare screen that lists every
feature with equal weight and default styling fails these checks even when every feature works (T-0026).
