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
if not s.slept: fail('no backoff via service.sleep')
if len(s.slept) < 4 or min(s.slept) <= 0: fail(f'expected >=4 positive backoffs, got {s.slept}')
try: client.fetch_all(MockService(always_fail=True)); fail('must raise ServiceDown')
except client.ServiceDown: pass

print('PASS')
