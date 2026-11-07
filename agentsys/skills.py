"""Skills: expert playbooks for the agents (the owner: "add strong dev and architect skills to the agents").

Each playbook in agentsys/playbooks/*.md is senior-level guidance for one kind of work (architecture, React front
ends, UI design, Python, testing, PWA reliability). Code - not the model - picks the ones that match a call, and only
the part for that role: a playbook may have "## For the Architect" / "## For the Developer" / "## For the Tester"
sections, and a role receives the shared part plus its own section only.

What a call is matched on (the benchmark brief: "an unrelated work item should receive only the guidance it needs"):
- the Architect and the FINAL regression: the whole requirement (every criterion matters);
- a Developer or Tester work item: that item (title, files, criteria) and the plan's tech choices - not the overall
  goal, so a theme item of an offline app does not get the offline playbook.

They are reviewed in git like the prompts, so they are trusted guidance - but they never override the criteria, the
constraints, the role's rules or the output format (the prompts say so). Bounded: at most MAX_PER_CALL playbooks and
MAX_CHARS characters per call. No extra model calls.
"""
import json
import re
from pathlib import Path

DIR = Path(__file__).resolve().parent / "playbooks"
MAX_PER_CALL = 3
MAX_CHARS = 16000
ROLE_SECTION = re.compile(r"^## For the (Architect|Developer|Tester)\s*$", re.M)
_cache = {}


def _load():
    files = sorted(DIR.glob("*.md"))
    sig = tuple((p.name, p.stat().st_mtime_ns) for p in files)
    if _cache.get("sig") != sig:
        books = []
        for p in files:
            text = p.read_text(encoding="utf-8")
            if not text.startswith("---"):
                continue
            _, fm, body = text.split("---", 2)
            meta = {}
            for line in fm.strip().splitlines():
                k, _, v = line.partition(":")
                meta[k.strip()] = [x.strip().lower() for x in v.split(",") if x.strip()]
            books.append({"name": (meta.get("name") or [p.stem])[0], "roles": meta.get("roles", []),
                          "always": meta.get("always", []), "keywords": meta.get("keywords", []), "text": body.strip()})
        _cache.update(sig=sig, books=books)
    return _cache["books"]


def _for_role(text, role):
    """The shared part of a playbook plus the section for this role; other roles' sections are left out."""
    parts = ROLE_SECTION.split(text)
    out = [parts[0].rstrip()]
    for i in range(1, len(parts), 2):
        if parts[i].lower() == role:
            out.append(f"## For the {parts[i]}\n{parts[i + 1].strip()}")
    return "\n\n".join(p for p in out if p).strip()


def _hits(keyword, text):
    if re.fullmatch(r"[a-z0-9 -]+", keyword):                      # whole words: "ui" must not match "build"
        return len(re.findall(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text))
    return text.count(keyword)                                      # e.g. ".py", "sw.js"


def context_for(role, requirement, payload):
    """The text a call is matched on (see the module docstring)."""
    item = (payload or {}).get("work_item")
    if role in ("developer", "tester") and isinstance(item, dict) and item.get("id") != "FINAL":
        return json.dumps([item, payload.get("tech_choices", []), payload.get("files_changed", [])], ensure_ascii=False)
    return json.dumps([requirement, payload], ensure_ascii=False)


def select(role, context):
    """The playbooks for one call: those `always` given to this role, then the best keyword matches among the ones
    for this role, each cut to the shared part + this role's section. Returns [{"name", "text"}], bounded."""
    text = str(context or "").lower()
    chosen, scored = [], []
    for b in _load():
        if role not in b["roles"]:
            continue
        if role in b["always"]:
            chosen.append(b)
            continue
        score = sum(_hits(k, text) for k in b["keywords"])
        if score:
            scored.append((score, b["name"], b))
    chosen += [b for _, _, b in sorted(scored, key=lambda x: (-x[0], x[1]))]
    out, used = [], 0
    for b in chosen[:MAX_PER_CALL]:
        body = _for_role(b["text"], role)
        if used + len(body) > MAX_CHARS:
            break
        out.append({"name": b["name"], "text": body})
        used += len(body)
    return out
