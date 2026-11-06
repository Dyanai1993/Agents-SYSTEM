"""Agent runner: one interface, two interchangeable backends (+ a fake one for tests/evals).

    run_agent(role, input, model, limits, task_id=...) -> AgentResult

Backends:
  sdk   Claude Agent SDK with the subscription login (`claude login`). Needs `pip install claude-agent-sdk`.
  cli   `claude -p --output-format stream-json` subprocess.
  fake  Canned answers. Zero quota. Used by unit tests and eval self-checks.
"""
import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import approvals, billing_guard, config, logger, prompts, schema
from .ledger import BudgetExceeded, Ledger, Window


class UsageLimitReached(RuntimeError):
    """The plan's usage window is used up. Save state and stop. Never retry in a loop."""


@dataclass
class Limits:
    max_turns: int = 20
    timeout_s: int = 900


@dataclass
class AgentResult:
    role: str
    ok: bool
    text: str = ""
    output: object = None
    usage: dict = field(default_factory=dict)
    duration_s: float = 0.0
    backend: str = ""
    api_key_source: str = None
    error: str = None
    init: dict = None
    tool_log: list = None      # [{tool, command, output, is_error}] or None if the backend cannot provide it


_LIMIT_PAT = re.compile(r"(usage limit|limit reached|rate.?limit|5-hour limit|weekly limit|out of extra usage)", re.I)


def looks_like_limit(text: str) -> bool:
    return bool(_LIMIT_PAT.search(text or ""))


def extract_json(text: str):
    """Pull one JSON object out of agent text: whole text, a ```json block, or first{..last}."""
    text = (text or "").strip()
    for cand in (text,):
        try:
            return json.loads(cand)
        except ValueError:
            pass
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except ValueError:
            pass
    a, b = text.find("{"), text.rfind("}")
    if a != -1 and b > a:
        try:
            return json.loads(text[a:b + 1])
        except ValueError:
            pass
    raise ValueError("no JSON object found in agent output")


# ---------------------------------------------------------------- backends
class FakeBackend:
    """responses: role -> dict | str | Exception | callable(prompt)->dict|str."""
    name = "fake"

    def __init__(self, responses=None, api_key_source="none", tool_logs=None):
        self.responses = responses or {}
        self.tool_logs = tool_logs          # role -> list; None = derive from output.checks[].command
        self.api_key_source = api_key_source
        self.calls = []

    def run(self, *, role, prompt, model, limits, system_prompt, tools, cwd, env):
        self.calls.append({"role": role, "prompt": prompt, "model": model})
        r = self.responses.get(role, {})
        if isinstance(r, list):          # scripted sequence: the last item repeats
            r = r.pop(0) if len(r) > 1 else r[0]
        if callable(r):
            r = r(prompt)
        if isinstance(r, BaseException):
            raise r
        text = r if isinstance(r, str) else json.dumps(r)
        if self.tool_logs is not None and role in self.tool_logs:
            tl = self.tool_logs[role]
        elif isinstance(r, dict) and role == "tester":
            tl = [{"tool": "Bash", "command": c.get("command", ""), "output": c.get("evidence", ""), "is_error": False}
                  for c in r.get("checks", []) if c.get("command")]
        else:
            tl = []
        return AgentResult(tool_log=tl, role=role, ok=True, text=text, backend=self.name, api_key_source=self.api_key_source,
                           usage={"input_tokens": len(prompt) // 4, "output_tokens": len(text) // 4, "turns": 1},
                           init={"apiKeySource": self.api_key_source})


class CliBackend:
    name = "cli"

    def __init__(self, binary="claude"):
        self.binary = binary

    def run(self, *, role, prompt, model, limits, system_prompt, tools, cwd, env):
        exe = shutil.which(self.binary, path=(env or os.environ).get("PATH"))
        if not exe:
            return AgentResult(role=role, ok=False, backend=self.name, error=f"'{self.binary}' not found on PATH")
        tool_list = ",".join(tools or [])
        cmd = [exe, "-p", "--output-format", "stream-json", "--verbose", "--model", model,
               "--max-turns", str(limits.max_turns),
               "--tools", tool_list,                     # AVAILABLE tools (empty string = none). --allowedTools alone only skips prompts
               "--strict-mcp-config"]                    # no MCP servers unless explicitly configured
        if tool_list:
            cmd += ["--allowedTools", tool_list]
        if system_prompt:
            cmd += ["--append-system-prompt", system_prompt]
        t0 = time.time()
        import tempfile
        import threading
        err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file, text=True, cwd=cwd, env=env)
        timed_out = threading.Event()

        def kill_on_timeout():
            timed_out.set()
            p.kill()
        timer = threading.Timer(limits.timeout_s, kill_on_timeout)
        timer.start()

        def feed():
            try:
                p.stdin.write(prompt)
                p.stdin.close()
            except (OSError, ValueError):
                pass
        threading.Thread(target=feed, daemon=True).start()
        init, result = None, None
        calls, outputs = {}, {}
        try:
            for line in p.stdout:                       # streamed: the credential source is checked the moment init arrives
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if msg.get("type") == "system" and msg.get("subtype") == "init":
                    init = msg
                    problems = billing_guard.check_init_message(msg)
                    if problems:
                        p.kill()                        # stop before the agent does any work
                        raise billing_guard.BillingGuardError(problems)
                elif msg.get("type") == "result":
                    result = msg
                elif msg.get("type") in ("assistant", "user"):
                    content = (msg.get("message") or {}).get("content")
                    for blk in content if isinstance(content, list) else []:
                        if not isinstance(blk, dict):
                            continue
                        if blk.get("type") == "tool_use":
                            calls[blk.get("id")] = {"tool": blk.get("name"), "command": (blk.get("input") or {}).get("command", "")}
                        elif blk.get("type") == "tool_result":
                            out = blk.get("content")
                            if isinstance(out, list):
                                out = " ".join(b.get("text", "") for b in out if isinstance(b, dict))
                            outputs[blk.get("tool_use_id")] = (str(out or "")[:4000], bool(blk.get("is_error")))
            p.wait()
        finally:
            timer.cancel()
            if p.poll() is None:
                p.kill()
        err_file.seek(0)
        stderr = err_file.read()
        err_file.close()
        if timed_out.is_set():
            return AgentResult(role=role, ok=False, backend=self.name, error=f"timeout after {limits.timeout_s}s", duration_s=time.time() - t0)
        tool_log = [dict(c, output=outputs.get(i, ("", False))[0], is_error=outputs.get(i, ("", False))[1]) for i, c in calls.items()]
        blob = (stderr or "") + " " + json.dumps(result or {})
        if (result is None or result.get("is_error")) and looks_like_limit(blob):
            raise UsageLimitReached(blob[:300])
        if result is None or result.get("is_error"):
            return AgentResult(role=role, ok=False, backend=self.name, init=init, duration_s=time.time() - t0,
                               error=(stderr or str(result))[:300])
        u = result.get("usage") or {}
        return AgentResult(role=role, ok=True, text=result.get("result", ""), backend=self.name, init=init, tool_log=tool_log,
                           api_key_source=(init or {}).get("apiKeySource"), duration_s=time.time() - t0,
                           usage={"input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                                  "output_tokens": u.get("output_tokens", 0), "turns": result.get("num_turns", 1)})


class SdkBackend:
    """Claude Agent SDK. NOT exercised by unit tests (needs a live login). Verify in Phase 2."""
    name = "sdk"

    def run(self, *, role, prompt, model, limits, system_prompt, tools, cwd, env):
        import asyncio
        try:
            import claude_agent_sdk as sdk
        except ImportError as e:
            return AgentResult(role=role, ok=False, backend=self.name, error=f"claude-agent-sdk not installed: {e}")

        async def go():
            opts = sdk.ClaudeAgentOptions(system_prompt=system_prompt or None, allowed_tools=list(tools or []),
                                          model=model, max_turns=limits.max_turns, cwd=str(cwd) if cwd else None,
                                          env=dict(env or {}))
            init, result = None, None
            async for msg in sdk.query(prompt=prompt, options=opts):
                name = type(msg).__name__
                if name == "SystemMessage" and getattr(msg, "subtype", "") == "init":
                    init = dict(getattr(msg, "data", {}) or {})
                elif name == "ResultMessage":
                    result = msg
            return init, result

        t0 = time.time()
        try:
            init, res = asyncio.run(asyncio.wait_for(go(), limits.timeout_s))
        except asyncio.TimeoutError:
            return AgentResult(role=role, ok=False, backend=self.name, error=f"timeout after {limits.timeout_s}s")
        except Exception as e:  # SDK raises process errors for limit hits
            if looks_like_limit(str(e)):
                raise UsageLimitReached(str(e)[:300])
            return AgentResult(role=role, ok=False, backend=self.name, error=str(e)[:300])
        if res is None or getattr(res, "is_error", False):
            txt = str(getattr(res, "result", "") or "")
            if looks_like_limit(txt):
                raise UsageLimitReached(txt[:300])
            return AgentResult(role=role, ok=False, backend=self.name, init=init, error=txt[:300] or "no result")
        u = getattr(res, "usage", None) or {}
        return AgentResult(role=role, ok=True, text=getattr(res, "result", "") or "", backend=self.name, init=init,
                           api_key_source=(init or {}).get("apiKeySource"), duration_s=time.time() - t0,
                           usage={"input_tokens": u.get("input_tokens", 0), "output_tokens": u.get("output_tokens", 0),
                                  "turns": getattr(res, "num_turns", 1)})


BACKENDS = {"fake": FakeBackend, "cli": CliBackend, "sdk": SdkBackend}


def get_backend(name=None):
    name = name or os.environ.get("AGENTSYS_BACKEND", "sdk")
    return BACKENDS[name]()


# ---------------------------------------------------------------- the interface
def run_agent(role, input, model=None, limits=None, *, task_id, run_id="r0", backend=None, cwd=None,
              env=None, settings_paths=None, system_prompt=None, post_process=None) -> AgentResult:
    """Guard -> budget check -> backend -> init check -> ledger -> schema check -> log. Sequential, no retries."""
    env = dict(os.environ if env is None else env)
    spec = config.ROLES.get(role, {"model": "sonnet", "tools": [], "schema": None})
    model = model or spec["model"]
    limits = limits or Limits(**config.ROLE_LIMITS.get(role, {}))
    backend = backend or get_backend()
    ledger = Ledger(task_id)
    if system_prompt is None:
        try:
            system_prompt = prompts.load_role(role)[0]
        except FileNotFoundError:
            system_prompt = None

    if settings_paths is None:        # check the settings the agent process will really read: ours AND its working directory's
        settings_paths = billing_guard.default_settings_paths(os.getcwd()) + (billing_guard.default_settings_paths(cwd)[1:] if cwd else [])
    billing_guard.run_guard(env=env, settings_paths=settings_paths)   # every call, not only at startup
    if model == "opus":
        approvals.require_opus(task_id)                               # Opus only with a human decision file
    ledger.check_can_call()                                           # raises BudgetExceeded

    prompt = json.dumps(input, ensure_ascii=False, indent=2) if not isinstance(input, str) else input
    log = lambda et, summary, **kw: logger.log_event(task_id, role, et, summary, run_id=run_id, model=model, **kw)
    log("agent_start", f"{role} starts on {model} via {backend.name}", from_="orchestrator", to=role, payload={"input": input})
    try:
        res = backend.run(role=role, prompt=prompt, model=model, limits=limits, system_prompt=system_prompt,
                          tools=spec["tools"], cwd=cwd, env=env)
    except UsageLimitReached as e:
        ledger.record_limit_hit(e)
        ckpt = config.home() / "tasks" / task_id / "checkpoint.json"
        ckpt.write_text(json.dumps({"reason": "usage_limit", "role": role, "run_id": run_id, "input": input,
                                    "ts": logger.now(), "resume": "re-run the same step after the window resets"},
                                   indent=2, default=str), encoding="utf-8")
        log("limit_hit", "usage limit reached: state saved, stopping", status="blocked", payload={"error": str(e)})
        raise

    tokens = res.usage.get("input_tokens", 0) + res.usage.get("output_tokens", 0)
    problems = billing_guard.check_init_message(res.init) if res.ok or res.init else []
    if problems:   # wrong credential source seen at runtime: stop everything
        log("alert", "billing guard: " + problems[0], status="blocked", payload={"problems": problems})
        raise billing_guard.BillingGuardError(problems)

    ledger.record_call(role, model, tokens)
    if not res.ok:
        res.error = res.error or "agent failed"
        log("error", f"{role} failed: {res.error}", status="error", tokens_estimate=tokens, payload={"error": res.error})
        return res

    if spec["schema"]:
        try:
            res.output = extract_json(res.text)
            schema.validate(spec["schema"], res.output)
        except (ValueError, schema.SchemaError) as e:
            res.ok, res.error = False, f"schema_validation: {e}"
            log("error", f"{role} output failed schema '{spec['schema']}'", status="error", tokens_estimate=tokens,
                payload={"error": str(e), "text": res.text[:4000]})
            return res
    else:
        try:
            res.output = extract_json(res.text)
        except ValueError:
            res.output = {"text": res.text}
    if res.tool_log:
        log("tool_call", f"{len(res.tool_log)} tool call(s) recorded", payload=res.tool_log)
    if post_process is not None:
        errs = post_process(res)
        if errs:
            res.ok, res.error = False, "semantic_validation: " + "; ".join(errs)
            log("error", f"{role} output failed semantic checks", status="error", tokens_estimate=tokens,
                payload={"errors": errs, "output": res.output})
            return res
    log("message", f"{role} -> orchestrator ({spec['schema'] or 'free text'})", from_=role, to="orchestrator",
        payload=res.output)     # tokens are counted once per call: on agent_end (or on the error event)
    log("agent_end", f"{role} done in {res.duration_s:.1f}s", tokens_estimate=tokens)
    return res
