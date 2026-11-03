import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
import hashlib
orig = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures', 'test_pricing.py'),'rb').read()
if open(os.path.join(WS,'test_pricing.py'),'rb').read() != orig: fail('test file was changed')
r = subprocess.run([sys.executable, '-m', 'unittest', 'test_pricing'], cwd=WS, capture_output=True, text=True)
if r.returncode: fail(r.stderr[-300:])

print('PASS')
