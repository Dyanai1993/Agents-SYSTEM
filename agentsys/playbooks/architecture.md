---
name: architecture
roles: architect
always: architect
keywords: architecture, design, system, module, service, app, application
---
# Architecture playbook (senior level)

## Start from the domain, not the screens
- Name the core entities, who owns them, and their invariants (what must always be true). Design the data model and
  its versioning first; screens and commands are views over it.
- Write the contracts between parts before the parts: types/schemas for data, function signatures for modules,
  file formats. Independent work items then build against the same contract.

## Boundaries
- Keep domain logic pure (no I/O, no DOM, no clock): it is then easy to test and to reuse.
- Put I/O at the edges behind small interfaces (storage, network, file system, time). Dependencies point inward.
- One module, one reason to change. Avoid both god-files and premature layers: a folder per feature, shared code
  only when two features really share it.

## State and flows
- One source of truth per piece of data; derive the rest, never duplicate it.
- Model multi-step flows as explicit states (idle -> loading -> success | error), not scattered booleans.
- Decide the error strategy up front: where errors are caught, what the user sees, what is retried, what is logged.
  Validate input at the boundary; never swallow an error.

## Planning work items
- Vertical slices that each end in something visible and testable. Order: foundation (data model, storage, design
  tokens, app shell) -> features -> hardening and polish. For any UI, the design system is an early item.
- A few substantial items beat many tiny ones: each item costs a build and a test call.
- Each item has a clear done-state and checkable criteria about behaviour AND quality: error and empty states,
  keyboard access, contrast, responsive widths (390px / 1280px), performance budgets where they matter.

## Choices
- Prefer boring, well-supported tools and the standard library; justify every dependency in one line.
- Name the hard parts as risks (timing, concurrency, browser support, data migration) and plan a test for each.

## Non-functional checklist (consider each, plan what applies)
Security (input validation, no secrets in code), accessibility (WCAG 2.2 AA), i18n and RTL, offline and caching,
performance, observability (useful logs and error messages), testability, upgrade/migration of stored data.
