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


def _cap_output(text, limit=4000):
    """Review (fourth round): truncating to the first `limit` characters silently dropped real evidence
    that sits at the END of a long tool output - e.g. unittest's own summary line ("Ran 50 tests ... OK"),
    printed after all the per-test output, was simply gone past 4000 characters. semantics.evidence_errors()
    then had nothing to match an honest, accurate quote against, and rejected a correct report as if it
    were fabricated. Keeping head AND tail (half the budget each) means a long run's final summary survives
    truncation just as reliably as its first lines do."""
    text = str(text or "")
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n...[truncated]...\n" + text[-half:]


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
#  Phase 5 added a new mandatory call (security) at the end of every task. Defaulting it here, the same way
# "tester" already gets special-cased just below for tool_logs, means a test FakeBackend built for something
# else entirely does not also need to configure a security response just to reach "ready_for_review".
_DEFAULT_FAKE_RESPONSE = {"security": {"verdict": "pass", "summary": "no findings", "findings": []}}


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
        r = self.responses.get(role, _DEFAULT_FAKE_RESPONSE.get(role, {}))
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


if os.name == "nt":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _JobObjectExtendedLimitInformation = 9
    _PROCESS_SET_QUOTA = 0x0100
    _PROCESS_TERMINATE = 0x0001

    class _IO_COUNTERS(ctypes.Structure):
        _fields_ = [("ReadOperationCount", ctypes.c_uint64), ("WriteOperationCount", ctypes.c_uint64),
                    ("OtherOperationCount", ctypes.c_uint64), ("ReadTransferCount", ctypes.c_uint64),
                    ("WriteTransferCount", ctypes.c_uint64), ("OtherTransferCount", ctypes.c_uint64)]

    class _JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.POINTER(ctypes.c_ulong)), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class _JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", _JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", _IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    def _create_job_for(pid):
        """A Windows Job Object containing `pid` and (by default) every process it ever spawns, with
        KILL_ON_JOB_CLOSE so closing the handle tears the whole tree down. Unlike `taskkill /T`, which walks
        the tree by querying the ROOT pid's children at kill time, TerminateJobObject kills every process the
        job ever contained even if the root (our direct child) already exited - unblocking an orphaned
        grandchild that inherited our stdout pipe and is still holding it open. Returns None on any failure
        (missing privilege, process already gone): callers must still fall back to `taskkill /T`."""
        job = _kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        info = _JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(job, _JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)):
            _kernel32.CloseHandle(job)
            return None
        hproc = _kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        if not hproc:
            _kernel32.CloseHandle(job)
            return None
        try:
            if not _kernel32.AssignProcessToJobObject(job, hproc):
                _kernel32.CloseHandle(job)
                return None
        finally:
            _kernel32.CloseHandle(hproc)
        return job

    def _kill_job(job):
        if job:
            _kernel32.TerminateJobObject(job, 1)
            _kernel32.CloseHandle(job)
else:
    def _create_job_for(pid):
        return None

    def _kill_job(job):
        pass


def _kill_tree(p, job=None):
    """Kill the process AND its children. On Windows `claude` may be a .cmd shim: killing only the shim leaves the real
    process holding our pipe open until it finishes. On POSIX the process runs in its own group.

    Started with start_new_session=True, so p.pid IS the process group id by construction - pass it to killpg
    directly instead of looking it up with os.getpgid(p.pid) first. That lookup needs p itself to still be a
    resolvable process; if p already exited (e.g. it crashed right after forking a child that inherited our
    stdout pipe and kept running), getpgid fails and nothing is signalled, so the orphaned child keeps the
    pipe open and our read loop blocks forever. killpg on the numeric group id works even once p is gone, as
    long as any member of that group (the orphan) is still alive.

    On Windows, `job` (from _create_job_for, assigned right after Popen) is tried FIRST for the same reason:
    `taskkill /T` needs p.pid to still resolve to walk its tree, which fails exactly when p already exited and
    left an orphan behind. The job object has no such dependency. `taskkill /T` still runs as a fallback for
    any process the job did not catch (e.g. one that already broke away before assignment)."""
    import signal
    try:
        if os.name == "nt":
            _kill_job(job)
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
        else:
            os.killpg(p.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        p.kill()
    except OSError:
        pass


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
        # Review (fourth round): err_file (and, if Popen itself fails after spawning but before job/timer/the
        # feeder thread are set up, the orphaned child process p) used to be created BEFORE the try/finally
        # below even starts - a Popen failure, or any exception from _create_job_for, left the stderr file
        # handle (and possibly a live orphaned process) leaked with nothing to ever close or kill it.
        err_file = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        p = job = timer = None
        try:
            # UTF-8 both ways, never the Windows code page: with text=True alone the prompt was encoded as cp1252, so a
            # Hebrew message (or an arrow in a pasted spec) could not be written at all, and UTF-8 output was misread.
            # Found live (first Requirements Assistant session).
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err_file, text=True,
                                 encoding="utf-8", errors="replace", cwd=cwd, env=env, start_new_session=(os.name != "nt"))
            job = _create_job_for(p.pid) if os.name == "nt" else None  # assigned ASAP: see _create_job_for's docstring
            timed_out = threading.Event()

            def kill_on_timeout():
                timed_out.set()
                _kill_tree(p, job)
            timer = threading.Timer(limits.timeout_s, kill_on_timeout)
            timer.start()

            feed_error = []

            def feed():
                try:
                    p.stdin.write(prompt)
                except (OSError, ValueError) as e:      # recorded, not swallowed: it explains a CLI that got no input
                    feed_error.append(f"{type(e).__name__}: {e}")
                try:
                    p.stdin.close()
                except (OSError, ValueError):
                    pass
            feeder = threading.Thread(target=feed, daemon=True)
            feeder.start()
        except Exception:
            if timer is not None:
                timer.cancel()
            if p is not None:
                _kill_tree(p, job)
                try:
                    p.wait(timeout=10)
                except (subprocess.TimeoutExpired, OSError):
                    pass
            err_file.close()
            raise
        init, result = None, None
        calls, outputs = {}, {}
        stderr = ""
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
                        _kill_tree(p, job); job = None  # stop before the agent does any work; never close this handle again (see finally)
                        raise billing_guard.BillingGuardError(problems)
                elif msg.get("type") == "result":
                    if init is None:          # stream completed (or skipped straight to a result) without ever
                        _kill_tree(p, job); job = None     # proving the credential source: fail closed, same as a bad init
                        raise billing_guard.BillingGuardError(["no init message received before the result: cannot prove the credential source"])
                    result = msg
                elif msg.get("type") in ("assistant", "user"):
                    if init is None:          # real agent output arrived before any init message: the process did
                        _kill_tree(p, job); job = None     # work with an unverified credential source - stop it now, not after
                        raise billing_guard.BillingGuardError(["no init message received before agent output: cannot prove the credential source"])
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
                            outputs[blk.get("tool_use_id")] = (_cap_output(out), bool(blk.get("is_error")))
            p.wait()
        finally:
            timer.cancel()
            if p.poll() is None:
                _kill_tree(p, job)
            elif job:
                _kernel32.CloseHandle(job)   # process exited on its own: release the job handle, never used to kill
            try:
                p.wait(timeout=10)                      # reap the process: no zombies
            except (subprocess.TimeoutExpired, OSError):
                pass
            for f in (p.stdout, p.stdin):               # no leaked handles, also on the error paths
                try:
                    f.close()
                except (OSError, ValueError):
                    pass
            try:
                err_file.seek(0)
                stderr = err_file.read()
            except (OSError, ValueError):
                pass
            err_file.close()
        if timed_out.is_set():
            return AgentResult(role=role, ok=False, backend=self.name, error=f"timeout after {limits.timeout_s}s", duration_s=time.time() - t0)
        tool_log = [dict(c, output=outputs.get(i, ("", False))[0], is_error=outputs.get(i, ("", False))[1]) for i, c in calls.items()]
        blob = (stderr or "") + " " + json.dumps(result or {})
        if (result is None or result.get("is_error")) and looks_like_limit(blob):
            raise UsageLimitReached(blob[:300])
        if result is None or result.get("is_error"):
            feeder.join(timeout=2)                      # a write that failed when the CLI exited: let it be recorded
            why = (f"the prompt could not be sent ({feed_error[0]}); " if feed_error else "") + (stderr or str(result))
            return AgentResult(role=role, ok=False, backend=self.name, init=init, duration_s=time.time() - t0, error=why[:300])
        u = result.get("usage") or {}
        return AgentResult(role=role, ok=True, text=result.get("result", ""), backend=self.name, init=init, tool_log=tool_log,
                           api_key_source=(init or {}).get("apiKeySource"), duration_s=time.time() - t0,
                           usage={"input_tokens": u.get("input_tokens", 0) + u.get("cache_creation_input_tokens", 0),
                                  "output_tokens": u.get("output_tokens", 0), "turns": result.get("num_turns", 1)})


def build_sdk_options(sdk, *, system_prompt, tools, model, limits, cwd, env):
    """Split out so unit tests can verify it against a real installed `claude_agent_sdk` (structural
    check only - no network, no login needed) without running the async query itself.

    `tools` restricts which tools EXIST (matches CliBackend's --tools); `allowed_tools` only auto-approves
    prompts for tools already available, the same split the CLI makes between --tools and --allowedTools.
    Passing only allowed_tools (as this used to) left every built-in tool available regardless of `tools`
    given to this call. strict_mcp_config matches --strict-mcp-config."""
    return sdk.ClaudeAgentOptions(system_prompt=system_prompt or None, tools=list(tools or []),
                                  allowed_tools=list(tools or []), strict_mcp_config=True,
                                  model=model, max_turns=limits.max_turns, cwd=str(cwd) if cwd else None,
                                  env=dict(env or {}))


class SdkBackend:
    """Claude Agent SDK. NOT exercised live by unit tests (needs a live login); options construction IS
    tested against a real installed SDK when available (optional dependency).

    Confirmed unsupported for real tasks (live-verified 2026-11-06, logs/smoke-1793988330.jsonl): the
    subscription login, --tools restriction and --strict-mcp-config all work correctly, but run() below
    never populates AgentResult.tool_log, so a real Bash call the agent made showed 0 tool log entries -
    evidence verification (semantics.evidence_errors) cannot work on this backend as built. No real entry
    point uses it by default (see get_backend()); do not use it for a real task."""
    name = "sdk"

    def run(self, *, role, prompt, model, limits, system_prompt, tools, cwd, env):
        import asyncio
        try:
            import claude_agent_sdk as sdk
        except ImportError as e:
            return AgentResult(role=role, ok=False, backend=self.name, error=f"claude-agent-sdk not installed: {e}")

        async def go():
            opts = build_sdk_options(sdk, system_prompt=system_prompt, tools=tools, model=model, limits=limits, cwd=cwd, env=env)
            init, result = None, None
            agen = sdk.query(prompt=prompt, options=opts)
            try:
                async for msg in agen:
                    name = type(msg).__name__
                    if name == "SystemMessage" and getattr(msg, "subtype", "") == "init":
                        init = dict(getattr(msg, "data", {}) or {})
                        problems = billing_guard.check_init_message(init)
                        if problems:
                            raise billing_guard.BillingGuardError(problems)   # stop before the agent does any work
                        continue
                    if name in ("ResultMessage", "AssistantMessage", "UserMessage") and init is None:
                        raise billing_guard.BillingGuardError(["no init message received before agent output: cannot prove the credential source"])
                    if name == "ResultMessage":
                        result = msg
            finally:
                try:
                    await agen.aclose()
                except Exception:
                    pass          # never let cleanup hide the real error raised above
            return init, result

        t0 = time.time()
        try:
            init, res = asyncio.run(asyncio.wait_for(go(), limits.timeout_s))
        except asyncio.TimeoutError:
            return AgentResult(role=role, ok=False, backend=self.name, error=f"timeout after {limits.timeout_s}s")
        except billing_guard.BillingGuardError:
            raise                    # never swallow this into a generic error string - the caller must see it
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
    # Still open (noted since the second review): SdkBackend has never been exercised live (needs a real
    # login) and builds no tool log, so it fails closed on every evidence check by design. Every real entry
    # point (orch.py, run_task.py, evals/run_evals.py, live_smoke.py) explicitly defaults its own --backend
    # to "cli", so this fallback is never actually hit by them - but it used to silently default to the
    # unverified "sdk" for any FUTURE direct caller that forgets to pass one. Defaulting here to "cli" too
    # keeps that consistent instead of being a footgun waiting for a new call site.
    name = name or os.environ.get("AGENTSYS_BACKEND", "cli")
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
        settings_paths = (billing_guard.default_settings_paths(os.getcwd(), env=env)
                          + (billing_guard.default_settings_paths(cwd, env=env)[1:] if cwd else []))
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

    ledger.record_call(role, model, tokens, res.duration_s)
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
