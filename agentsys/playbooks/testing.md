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

## Procedure: a check that proves something
1. Drive the real code path: real clicks, real key presses, real handlers. Never intercept events before the app
   (a capture-phase listener with `stopPropagation()` proves only that keys produce events - T-0026's keyboard test
   did exactly this and the app's handlers never ran).
2. Assert the observable outcome: the text, state or file the user would see, and where focus ends up.
3. Prove the check can fail: break the behaviour on purpose (or name the exact broken implementation it rejects) and
   see the check fail. A check that cannot fail proves nothing.
4. Bind the result to its own run: the command you ran for THIS scenario and an excerpt of ITS output. Not evidence
   for a criterion: an unrelated command, a grep of a test's name, an aggregate count ("Ran 42 tests, OK"), or a
   file merely existing. Several scenarios may share one command when each has its own distinct line in the output.
5. A genuine expected failure is valid evidence when the scenario is "this must fail" (exit code 1, an error message).
6. Be honest about what was not performed: a phone install, a hosted HTTPS deployment, a visual judgement you did not
   make. Report those as not verified - never infer them (T-0026 counted `<link rel="manifest">` as proof the app
   installs on Android and iPhone).
