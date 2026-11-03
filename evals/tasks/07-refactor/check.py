import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
import ast
src = open(os.path.join(WS, 'messy.py')).read()
for n in ast.walk(ast.parse(src)):
    if isinstance(n, ast.FunctionDef) and n.end_lineno - n.lineno + 1 > 15: fail(f'{n.name} too long')
if src.count('0.2') + src.count('0.07') + src.count('0.17') > 3: fail('tax logic duplicated')
if not os.path.exists(os.path.join(WS, 'test_messy.py')): fail('tests missing')
r = subprocess.run([sys.executable, '-m', 'unittest', 'test_messy'], cwd=WS, capture_output=True, text=True)
if r.returncode: fail(r.stderr[-300:])

print('PASS')
