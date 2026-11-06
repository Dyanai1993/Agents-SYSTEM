import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
from mock_service import MockService
import client
s = MockService()
got = client.fetch_all(s)
if [r['id'] for r in got] != [1,2,3,4,5]: fail(f'records={got}')
if s.calls != {1: 3, 2: 3}: fail(f'the client must really call each page until it succeeds (2 failures + 1 success); calls={s.calls}')
class Other(MockService):                      # different data: a hard-coded answer cannot pass
    PAGES = {1: ([{'id': 10}, {'id': 11}], 2), 2: ([{'id': 12}], 3), 3: ([{'id': 13}], None)}
o = Other(fail_first=1)
if [r['id'] for r in client.fetch_all(o)] != [10, 11, 12, 13]: fail('client must read the service data, not return fixed ids')
if not s.slept: fail('no backoff via service.sleep')
if len(s.slept) < 4 or min(s.slept) <= 0: fail(f'expected >=4 positive backoffs, got {s.slept}')
try: client.fetch_all(MockService(always_fail=True)); fail('must raise ServiceDown')
except client.ServiceDown: pass

print('PASS')
