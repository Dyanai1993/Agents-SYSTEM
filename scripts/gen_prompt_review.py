"""Regenerate docs/agent-prompts-for-review.md from .claude/agents/*.md and schemas/. Run after any prompt change.
tests/test_loop.py::PromptTests fails when the committed file is out of date."""
import pathlib

root = pathlib.Path(__file__).resolve().parent.parent
DEST = root / "docs" / "agent-prompts-for-review.md"


def _read(rel):
    return (root / rel).read_text(encoding="utf-8")


def render():
    out = [_read("docs/prompt-review-header.md")]
    for role in ("assistant", "architect", "developer", "tester", "security"):
        out.append(f"\n---\n\n# Prompt: {role}  (`.claude/agents/{role}.md`)\n\n````markdown\n" + _read(f".claude/agents/{role}.md").rstrip() + "\n````\n")
    out.append("\n---\n\n# Playbooks (skills), chosen by code per call - see agentsys/skills.py\n")
    for p in sorted((root / "agentsys" / "playbooks").glob("*.md")):
        out.append(f"\n## {p.name}\n\n````markdown\n" + p.read_text(encoding="utf-8").rstrip() + "\n````\n")
    out.append("\n---\n\n# Output schemas the replies are validated against\n")
    for n in ("plan", "dev_report", "test_report", "security_report", "requirements"):
        out.append(f"\n## {n}.schema.json\n\n```json\n" + _read(f"schemas/{n}.schema.json").rstrip() + "\n```\n")
    out.append("\n---\n\n# Rules enforced in code, not only in the prompt\n\n```python\n" + _read("agentsys/semantics.py").rstrip() + "\n```\n")
    return "".join(out)


if __name__ == "__main__":
    text = render()
    DEST.write_text(text, encoding="utf-8")
    print("written", len(text.splitlines()), "lines")
