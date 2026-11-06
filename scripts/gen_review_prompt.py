"""Build the self-review prompt for a finished phase, to hand to an independent reviewer.

  python scripts/gen_review_prompt.py 3        reads docs/reviews/phase3.json, writes docs/reviews/phase3-review-prompt.md

The reviewer reviews ONLY. It must not change files. All changes are made by the builder (Claude Code).
"""
import json, subprocess, sys
from pathlib import Path

REPO_URL = "https://github.com/Dyanai1993/Agents-SYSTEM"
root = Path(__file__).resolve().parent.parent
n = sys.argv[1]
spec = json.loads((root / "docs" / "reviews" / f"phase{n}.json").read_text(encoding="utf-8"))

def git(*a):
    r = subprocess.run(["git", *a], cwd=root, capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


branch = spec.get("branch") or git("rev-parse", "--abbrev-ref", "HEAD") or "main"
links = [f"- Repository: {REPO_URL}", f"- Branch to review: {REPO_URL}/tree/{branch}"]
if spec.get("pr"):
    links.append(f"- Pull request (diff and discussion): {REPO_URL}/pull/{spec['pr']}")
links.append(f"- Compare with main: {REPO_URL}/compare/main...{branch}")
for f in spec["files"]:
    pass


def listing(items):
    return "\n".join(f"- {i}" for i in items)

files = []
for f in spec["files"]:
    p = root / f
    files.append(f"[{f}]({REPO_URL}/blob/{branch}/{f})  ({len(p.read_text(encoding='utf-8').splitlines())} lines)" if p.is_file() else f"{f}  (MISSING)")

out = f"""# Review request: Phase {n} — {spec['title']}

## Where the code is
{chr(10).join(links)}
Review the branch above, not main. Files are listed relative to the repository root; each one opens at `{REPO_URL}/blob/{branch}/<path>`.

## Your role
You are an independent REVIEWER. Review only. Do NOT edit, create, delete or commit any file. Do NOT run anything that spends
model quota or touches the network, and never use an API key. The builder (Claude Code) makes every change after reading your report.
If you cannot verify something, say "not verified" and why. Do not guess.

## System in one paragraph
{spec['context']}

## Hard constraints of the whole system (check every change against them)
- Billing: Claude Max subscription login only. No API key, no pay-per-token, no extra usage. The billing guard must stay fail-closed.
- Usage is the scarce resource: sequential agents, small contexts, hard loop limits, no tight retry loops.
- Least privilege and human approval for irreversible actions. The dashboard never calls an LLM.
- Agent output is validated by schemas AND code rules. A prompt rule that code does not enforce is a risk: report it.
- Personal use only.

## What this phase claims to deliver
{listing(spec['claims'])}

## Files to read (start here)
{listing(files)}

## Weak points the builder already suspects (confirm, refute, or rank them)
{listing(spec['known_weaknesses'])}

## How to verify without spending quota
{listing('`' + c + '`' for c in spec['verify'])}

## Review dimensions
1. Correctness: logic errors, wrong state transitions, races, resume/crash behaviour.
2. Billing and quota safety: any path that could call a paid API, loop on calls, or skip the guard or the caps.
3. Security: file-path handling, injection, the dashboard write actions, secrets in logs or files, prompt-injection paths.
4. Enforcement gap: rules in prompts or docs that no code or test enforces.
5. Test quality: tests that pass for the wrong reason, missing failure cases, tests that mirror the implementation.
6. Maintainability and honesty of docs: do README and docs say what the code does? Anything claimed but not built?
7. Specific questions for this phase:
{listing(spec['questions'])}

## Output format (use exactly this)
1. **Verdict**: one of `ship`, `ship after fixes`, `do not ship`, with one sentence.
2. **Findings table**: columns `#`, `severity (blocker/high/medium/low)`, `file:line`, `what is wrong`, `how to reproduce or verify`, `suggested fix (one line)`.
3. **Claims check**: for each claim above: `verified`, `not verified`, or `false`, with evidence.
4. **Missing tests**: bullet list of concrete test cases.
5. **Things you did NOT review** and why.
Keep it short. No praise. Rank by risk.
"""
dest = root / "docs" / "reviews" / f"phase{n}-review-prompt.md"
dest.write_text(out, encoding="utf-8")
print("written", dest.relative_to(root), len(out.splitlines()), "lines")
