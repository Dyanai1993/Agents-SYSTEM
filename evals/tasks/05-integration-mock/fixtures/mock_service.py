class Error503(Exception): pass
class MockService:
    PAGES = {1: ([{'id':1},{'id':2},{'id':3}], 2), 2: ([{'id':4},{'id':5}], None)}
    def __init__(self, fail_first=2, always_fail=False):
        self.fail_first, self.always_fail, self.calls, self.slept = fail_first, always_fail, {}, []
    def sleep(self, s): self.slept.append(s)
    def get_page(self, n):
        self.calls[n] = self.calls.get(n, 0) + 1
        if self.always_fail or self.calls[n] <= self.fail_first: raise Error503()
        recs, nxt = self.PAGES[n]; return {'records': recs, 'next': nxt}
