"""The PWA reliability benchmark (evals/pwa_benchmark): the same real-browser checks against a reference and a
deliberately broken implementation of one tiny Hebrew RTL PWA - the broken one reproduces T-0026's flaws. The
benchmark is OK only when the reference passes every check AND every flaw check fails on the broken build (a check
that cannot fail proves nothing). Zero model calls. Skipped when its Playwright is not installed:
    cd evals/pwa_benchmark && npm install   (PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 reuses an installed Chromium)"""
import json, shutil, subprocess, unittest
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent / "evals" / "pwa_benchmark"


class PwaBenchmarkTests(unittest.TestCase):
    def test_reference_passes_and_every_t0026_flaw_is_caught(self):
        if not (BENCH / "node_modules" / "playwright").is_dir() or not shutil.which("node"):
            self.skipTest("evals/pwa_benchmark has no Playwright installed (npm install there)")
        r = subprocess.run(["node", "check.mjs"], cwd=BENCH, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=400)
        out = json.loads((BENCH / "results" / "benchmark.json").read_text(encoding="utf-8"))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertTrue(out["reference_passes_all"])
        self.assertEqual(out["broken_flaws_caught"], "5/5")
        for name in ("1 broken B", "3 a corrupt file", "4 a ready marker", "5 keyboard"):
            broken = next(v for k, v in out["results"]["broken"].items() if k.startswith(name[:1]) and name.split(" ", 1)[1][:8] in k)
            self.assertFalse(broken["pass"], name)
        for note in out["visual"]["notes"]:
            self.assertIn("dir=rtl lang=he English tile ltr/en horizontal scroll false", note)

    def test_first_use_eval_separates_a_bare_shell_from_the_reference(self):
        """Benchmark phase 4: the reference meets all seven first-use criteria; a bare but functional shell (T-0026's
        first screen) fails one clear first action, guidance, hierarchy and styling; every mutant of the reference
        with one property broken fails the criterion it targets."""
        if not (BENCH / "node_modules" / "playwright").is_dir() or not shutil.which("node"):
            self.skipTest("evals/pwa_benchmark has no Playwright installed (npm install there)")
        r = subprocess.run(["node", "first_use.mjs"], cwd=BENCH, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=400)
        out = json.loads((BENCH / "results" / "first_use.json").read_text(encoding="utf-8"))
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-1000:])
        self.assertTrue(out["reference_passes_all"])
        self.assertEqual(out["bare_fails_first_use"], "4/4")
        self.assertEqual(out["mutants_caught"], "7/7")
        if "t0026" in out["report"]:      # the delivered T-0026 build, read-only, when it is on this machine
            self.assertFalse(out["report"]["t0026"]["FU1 one clear first action"]["pass"])
