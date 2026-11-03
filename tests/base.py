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
