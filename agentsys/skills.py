"""Skills: expert playbooks for the agents (the owner: "add strong dev and architect skills to the agents").

Each playbook in agentsys/playbooks/*.md is senior-level guidance for one kind of work (architecture, React front
ends, UI design, Python, testing). Code - not the model - picks the ones that match a call: the role, and keywords
in the requirement, the plan's tech choices and the work item. They go into the agent's input as `playbooks`.
They are reviewed in git like the prompts, so they are trusted guidance - but they never override the criteria,
the constraints, the role's rules or the output format (the prompts say so). Bounded: at most MAX_PER_CALL
playbooks and MAX_CHARS characters per call. No extra calls.
"""
import re
from pathlib import Path

DIR = Path(__file__).resolve().parent / "playbooks"
MAX_PER_CALL = 3
MAX_CHARS = 16000
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


def _hits(keyword, text):
    if re.fullmatch(r"[a-z0-9 -]+", keyword):                      # whole words: "ui" must not match "build"
        return len(re.findall(r"(?<![a-z0-9])" + re.escape(keyword) + r"(?![a-z0-9])", text))
    return text.count(keyword)                                      # e.g. ".py"


def select(role, context):
    """The playbooks for one call: those `always` given to this role, then the best keyword matches among the ones
    for this role. Returns [{"name", "text"}], bounded."""
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
        if used + len(b["text"]) > MAX_CHARS:
            break
        out.append({"name": b["name"], "text": b["text"]})
        used += len(b["text"])
    return out
