import sys
if len(sys.argv) != 2: sys.exit('usage: wc.py FILE')
try: t = open(sys.argv[1], encoding='utf-8').read()
except FileNotFoundError:
    print(f'wc.py: {sys.argv[1]}: No such file', file=sys.stderr); sys.exit(1)
print(len(t.splitlines()), len(t.split()), len(t), sys.argv[1])
