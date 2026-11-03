# Architecture (Phase 1)

```
 task text
    |
    v
 orchestrator (script; state machine arrives in Phase 3)
    |  run_agent(role, input, model, limits)
    v
 +--------------------------- runner.py ---------------------------+
 | 1 billing guard (env, settings)      4 init message: apiKeySource |
 | 2 budget ledger check (calls, time)  5 ledger + window update     |
 | 3 backend: sdk | cli | fake           6 schema check, event log    |
 +-------------------------------------------------------------------+
    |                                   |
    v                                   v
 tasks/<id>/ (state, budget, payloads)  logs/<id>.jsonl
                      \                /
                       v              v
                   dashboard (reads files only, 127.0.0.1)
```

Failure behaviour: usage limit -> `checkpoint.json` + `limit_hit` event + stop (no retry).
Bad agent output -> `error` event, result `ok=False`, orchestrator decides. Wrong credential source -> alert + stop.

## Not verified yet (needs your login)
- `SdkBackend` and the real `claude -p` stream-json format (`apiKeySource` in the init message). The CLI backend is tested
  against a fake `claude` script only. First live check happens at the start of Phase 2, with 1 Haiku call.
- Usage window sizes in `config.py` are guesses for Max 5x. They are calibrated from real limit hits.
