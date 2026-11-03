import sys, os, subprocess, json, importlib.util
WS = os.path.abspath(sys.argv[1])
sys.path.insert(0, WS)
def fail(m):
    print('FAIL:', m); sys.exit(1)
p = os.path.join(WS, 'report.json')
if not os.path.exists(p): fail('report.json missing')
r = json.load(open(p))
if abs(r.get('total_revenue', -1) - 57.5) > 1e-6: fail(f"total_revenue={r.get('total_revenue')} expected 57.5")
if r.get('top_product') != 'apple': fail(f"top_product={r.get('top_product')}")
if r.get('rows_skipped') != 2: fail(f"rows_skipped={r.get('rows_skipped')} expected 2")

print('PASS')
