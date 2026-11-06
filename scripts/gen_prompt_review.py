"""Regenerate docs/agent-prompts-for-review.md from .claude/agents/*.md and schemas/. Run after any prompt change."""
import pathlib
root = pathlib.Path(__file__).resolve().parent.parent
head = (root / "docs" / "prompt-review-header.md").read_text()
out = [head]
for role in ("architect", "developer", "tester", "security"):
    out.append(f"\n---\n\n# Prompt: {role}  (`.claude/agents/{role}.md`)\n\n````markdown\n" + (root / f".claude/agents/{role}.md").read_text().rstrip() + "\n````\n")
out.append("\n---\n\n# Output schemas the replies are validated against\n")
for n in ("plan", "dev_report", "test_report", "security_report", "requirements"):
    out.append(f"\n## {n}.schema.json\n\n```json\n" + (root / f"schemas/{n}.schema.json").read_text().rstrip() + "\n```\n")
out.append("\n---\n\n# Rules enforced in code, not only in the prompt\n\n```python\n" + (root / "agentsys/semantics.py").read_text().rstrip() + "\n```\n")
(root / "docs" / "agent-prompts-for-review.md").write_text("".join(out))
print("written", len("".join(out).splitlines()), "lines")
