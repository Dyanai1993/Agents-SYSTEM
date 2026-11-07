---
name: testing
roles: developer, tester
always: developer, tester
keywords: test, tests, testing, qa, vitest, jest, pytest, unittest, playwright
---
# Testing playbook (senior level)

## What to test
- Behaviour from the user's side, one scenario per test, named after the behaviour ("stop button pauses audio").
- The happy path, the edges (empty, one, many, maximum, Unicode and Hebrew text, RTL), the error paths (missing
  file, bad input, storage full, offline), and a regression test for every bug you fix.
- For UI: query by role and accessible name, interact like a user (click, type, keyboard), assert what is visible;
  check focus moves sensibly and every action works by keyboard; check the 390px and 1280px layouts in end-to-end
  tests when the project has Playwright.

## How
- Deterministic: no real network, no wall-clock sleeps (fake timers), fixed seeds, isolated temporary directories,
  no dependence on test order. Mock only at boundaries you do not own (network, time, browser storage).
- Fast: unit tests in milliseconds; keep the slow end-to-end tests few and meaningful.
- A test must be able to fail: make it fail once (or reason precisely why it would) before trusting it.

## Evidence
- Run the exact command and report its real output. Never claim a pass you did not see. One check per scenario,
  with the command as typed and an excerpt copied from its output.
