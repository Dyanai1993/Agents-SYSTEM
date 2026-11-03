def compute_total(orders):
    total = 0
    for o in orders:
        if o['region'] == 'EU':
            t = o['amount'] * 0.2
            total = total + o['amount'] + t
        elif o['region'] == 'US':
            t = o['amount'] * 0.07
            total = total + o['amount'] + t
        elif o['region'] == 'IL':
            t = o['amount'] * 0.17
            total = total + o['amount'] + t
        else:
            total = total + o['amount']
        if o.get('coupon'):
            total = total - 5
    return round(total, 2)
