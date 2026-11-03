"""Load agent definitions from .claude/agents/<role>.md (reviewed in git)."""
from pathlib import Path

AGENTS_DIR = Path(__file__).resolve().parent.parent / ".claude" / "agents"


def load_role(role: str):
    """Return (system_prompt, meta). meta has name, description, tools (list), model."""
    path = AGENTS_DIR / f"{role}.md"
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise ValueError(f"{path}: missing frontmatter")
    _, fm, body = text.split("---", 2)
    meta = {}
    for line in fm.strip().splitlines():
        k, _, v = line.partition(":")
        meta[k.strip()] = v.strip()
    meta["tools"] = [t.strip() for t in meta.get("tools", "").split(",") if t.strip()]
    return body.strip(), meta
