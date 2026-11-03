import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
import shutil
shutil.copy(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'sample.txt'), WS) if not os.path.exists(os.path.join(WS,'sample.txt')) else None
r = subprocess.run([sys.executable, 'wc.py', 'sample.txt'], cwd=WS, capture_output=True, text=True)
if r.stdout.strip() != '2 6 27 sample.txt': fail(f'stdout={r.stdout!r}')
r = subprocess.run([sys.executable, 'wc.py', 'nope.txt'], cwd=WS, capture_output=True, text=True)
if r.returncode != 1 or 'No such file' not in r.stderr: fail(f'missing file: rc={r.returncode} err={r.stderr!r}')

print('PASS')
