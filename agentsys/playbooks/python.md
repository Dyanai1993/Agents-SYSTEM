---
name: python
roles: architect, developer
keywords: python, .py, pip, pytest, unittest, argparse, cli, script, django, flask, fastapi
---
# Python playbook (senior level)

## Structure
- A package with modules by responsibility; a `main(argv=None)` entry point; no work at import time; core logic in
  pure functions that take data and return data; I/O at the edges.
- Type hints everywhere public; `dataclasses` (or `typing.NamedTuple`) for records; `pathlib.Path` for paths.

## Robustness
- Specific exceptions with actionable messages; never a bare `except`; context managers for files, locks and temp
  dirs. Validate input at the boundary and fail with a clear message, not a traceback.
- ALWAYS pass `encoding="utf-8"` to `open()`, `read_text()`, `write_text()` and text-mode `subprocess` calls: the
  Windows default code page breaks Hebrew and other non-ASCII text.
- `subprocess` with argument lists, never shell strings; timeouts on anything that can hang.

## CLIs
- `argparse` with help text for every option; exit codes 0 = success, 1 = failure, 2 = usage error; errors to
  stderr, results to stdout; `logging` (not print) for diagnostics, quiet by default.

## Dependencies
- Standard library first. A third-party package only when it clearly improves the result; pin it in
  `requirements.txt` (or `pyproject.toml`) and say why in one line.

## Tests
- `unittest` or `pytest`, deterministic: temporary directories, no network, fixed seeds, no sleeps. Test the CLI by
  calling `main([...])` or with `subprocess` and asserting exit code and output. One behaviour per test, named after
  the behaviour; a regression test for every bug fixed.
