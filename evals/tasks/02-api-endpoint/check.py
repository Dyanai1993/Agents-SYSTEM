import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
if not os.path.exists(os.path.join(WS,'test_app.py')): fail('test_app.py missing')
import app
s, b = app.handle('POST', '/items', {'name': 'x'})
if s != 201 or b.get('name') != 'x' or not isinstance(b.get('id'), int): fail(f'POST -> {s} {b}')
if app.handle('POST', '/items', {})[0] != 400: fail('missing name must be 400')
if app.handle('POST', '/items', {'name': '  '})[0] != 400: fail('blank name must be 400')
if app.handle('POST', '/items', {'name': 5})[0] != 400: fail('non-string name must be 400')
s, b = app.handle('GET', '/items')
if s != 200 or not any(i['name']=='x' for i in b['items']): fail('GET must list item')
if app.handle('GET', '/nope')[0] != 404: fail('unknown path must be 404')
r = subprocess.run([sys.executable, '-m', 'unittest', 'test_app'], cwd=WS, capture_output=True, text=True)
if r.returncode: fail('own tests fail: ' + r.stderr[-200:])

print('PASS')
