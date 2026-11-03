import os, shutil, tempfile, unittest
from pathlib import Path


class TmpHome(unittest.TestCase):
    """Each test runs in an empty AGENTSYS_HOME and a clean billing environment."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._saved = dict(os.environ)
        os.environ["AGENTSYS_HOME"] = self.tmp
        from agentsys import billing_guard
        for k in billing_guard.FORBIDDEN_ENV + billing_guard.FORBIDDEN_FLAGS:
            os.environ.pop(k, None)
        self.home = Path(self.tmp)
        self.no_settings = []   # do not read the real ~/.claude/settings.json in tests

    def tearDown(self):
        os.environ.clear(); os.environ.update(self._saved)
        shutil.rmtree(self.tmp, ignore_errors=True)


def fake_claude(home, body):
    """Create a fake `claude` executable in home/bin. Return an env dict whose PATH finds it.

    Works on Linux, macOS and Windows (claude.py + claude.cmd wrapper using the current Python).
    """
    import stat, sys
    bindir = Path(home) / "bin"
    bindir.mkdir(exist_ok=True)
    if os.name == "nt":
        (bindir / "claude.py").write_text(body, encoding="utf-8")
        (bindir / "claude.cmd").write_text(f'@echo off\r\n"{sys.executable}" "%~dp0claude.py" %*\r\n', encoding="utf-8")
    else:
        exe = bindir / "claude"
        exe.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    env = {"PATH": str(bindir) + os.pathsep + os.environ.get("PATH", "")}
    if os.name == "nt":
        env["SYSTEMROOT"] = os.environ.get("SYSTEMROOT", "")
        env["PATHEXT"] = os.environ.get("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    return env
