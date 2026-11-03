_items = []
def handle(method, path, body=None):
    if path != '/items': return 404, {'error': 'not found'}
    if method == 'GET': return 200, {'items': list(_items)}
    if method == 'POST':
        name = (body or {}).get('name')
        if not isinstance(name, str) or not name.strip(): return 400, {'error': 'name required'}
        it = {'id': len(_items) + 1, 'name': name}; _items.append(it); return 201, it
    return 405, {'error': 'method'}
