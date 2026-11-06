# Calibration data (live runs, Max 5x, 2026-11)

Source: `tasks/*/budget.json` from the owner's runs. Token counts are the SDK/CLI `usage` input + output.

| Task | Calls | Tokens | Fix loops | Notes |
|---|---|---|---|---|
| smoke (1 Haiku call) | 1 | 19,557 in + 94 out | - | fixed context of one call |
| T-0001 wc (blocked once, then resumed) | 5 | 108,508 | 0 | includes one tester call rejected by the evidence check |
| T-0002 wc (clean) | 4 | 86,826 | 0 | architect 1, developer 1, tester 2 (item + final) |
| T-0003 slugify | 4 | 85,771 | 0 | first-try pass: the fix loop was NOT exercised live |

## What it means
- A clean one-item task costs about **86k tokens**, i.e. about **21k per call**. Most of it is fixed context, so cost follows the number of calls, not the size of the code.
- Estimate before a task: `(2 x items + 2) x 21k x 1.25` weighted tokens (`ledger.estimate_task_units`).
- No limit was hit in about 300k tokens. The 5-hour and weekly window sizes in `config.py` are still guesses. They need a real limit hit, or 10+ tasks of data.
- Earlier numbers of 172k-194k came from a bug in `scripts/timeline.py`: `message` and `agent_end` events both carried the tokens. Fixed: tokens are logged once per call.

## Still unmeasured
- Cost of a fix loop and of a re-plan (no live run needed one yet).
- Opus calls, Haiku calls inside a task.
- The real window size for Max 5x.

## Update: restricting tools lowers the cost of a call (live smoke, 2026-11-06)
Same smoke call (1 Haiku call, 1 Bash command): **19,557 input tokens with the default tool set, 5,909 with `--tools Bash --strict-mcp-config`**.
The default tool definitions were about two thirds of the fixed context. The smoke role has only 1 tool; the Developer has 6, the Tester 4,
the Architect 3, so the saving per real call will be smaller. **`TOKENS_PER_CALL` (21k) is therefore probably too high now.**
It is NOT changed until a full task runs with the new flags (about 4 calls): then compare with the 86k per clean task above.
