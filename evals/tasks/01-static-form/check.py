import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
from html.parser import HTMLParser
p = os.path.join(WS, 'index.html')
if not os.path.exists(p): fail('index.html missing')
class P(HTMLParser):
    form = email = req = btn = False
    def handle_starttag(self, t, a):
        a = dict(a)
        if t == 'form': self.form = True
        if t == 'input' and a.get('name') == 'email': self.email = True; self.req = 'required' in a
        if t == 'button' and a.get('type', 'submit') == 'submit' or t == 'input' and a.get('type') == 'submit': self.btn = True
x = P(); x.feed(open(p).read())
if not (x.form and x.email and x.req and x.btn): fail(f'form={x.form} email={x.email} required={x.req} button={x.btn}')

print('PASS')
