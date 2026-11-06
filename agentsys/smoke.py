"""One cheap live check (1 Haiku call). Proves: subscription credential source, tool log format, usage capture."""
import tempfile
import time

from . import runner

def config_tools():
    from . import config
    return config.ROLES["smoke"]["tools"]


PROMPT = ('Use the Bash tool to run exactly: echo agentsys-smoke\n'
          'Then reply with ONLY this JSON: {"ok": true}')


def smoke(backend):
    """Return (ok, lines). Never raises for expected failures."""
    lines = []
    with tempfile.TemporaryDirectory() as tmp:
        res = runner.run_agent("smoke", PROMPT, task_id=f"smoke-{int(time.time())}", backend=backend, cwd=tmp)
    init = res.init or {}
    tools = init.get("tools")
    mcp = init.get("mcp_servers")
    allowed = set(config_tools())
    checks = [
        ("credential source is subscription", res.api_key_source in ("none", "oauth"), f"apiKeySource={res.api_key_source!r}"),
        ("agent answered", res.ok, res.error or "ok"),
        ("tool log captured", bool(res.tool_log), f"{len(res.tool_log or [])} tool call(s)"),
        ("bash command seen in tool log", any("agentsys-smoke" in (t.get("command") or "") for t in (res.tool_log or [])), "echo agentsys-smoke"),
        ("tool output captured", any("agentsys-smoke" in (t.get("output") or "") for t in (res.tool_log or [])), "output text"),
        ("only the allowed tools exist (--tools)", tools is None or set(tools) <= allowed,
         "init lists no tools field: NOT VERIFIED" if tools is None else f"tools={sorted(tools)}"),
        ("no MCP servers (--strict-mcp-config)", not mcp, "init lists no mcp_servers field: NOT VERIFIED" if mcp is None else f"mcp_servers={mcp}"),
        ("usage captured", sum(res.usage.values()) > 0 if res.usage else False, str(res.usage)),
    ]
    for name, ok, detail in checks:
        lines.append(f"{'PASS' if ok else 'FAIL'}  {name}  ({detail})")
    return all(ok for _, ok, _ in checks), lines
