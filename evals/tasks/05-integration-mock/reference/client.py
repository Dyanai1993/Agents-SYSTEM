from mock_service import Error503
class ServiceDown(Exception): pass
def fetch_all(service):
    out, seen, page = [], set(), 1
    while page:
        for attempt in range(5):
            try: data = service.get_page(page); break
            except Error503:
                if attempt == 4: raise ServiceDown()
                service.sleep(2 ** attempt)
        for r in data['records']:
            if r['id'] not in seen: seen.add(r['id']); out.append(r)
        page = data['next']
    return out
