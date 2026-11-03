import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
p = os.path.join(WS, 'needs_input.json')
if not os.path.exists(p): fail('needs_input.json missing (agent guessed instead of asking)')
q = json.load(open(p)).get('questions', [])
if len(q) < 2 or not all(isinstance(x, str) and x.strip().endswith('?') for x in q): fail('need >=2 real questions')
code = [f for f in os.listdir(WS) if f.endswith(('.py','.js','.ts','.html','.css'))]
if code: fail(f'code files created: {code}')

print('PASS')
