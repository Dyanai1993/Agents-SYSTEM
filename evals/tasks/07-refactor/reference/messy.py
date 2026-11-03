RATES = {'EU': 0.2, 'US': 0.07, 'IL': 0.17}
def with_tax(o):
    return o['amount'] * (1 + RATES.get(o['region'], 0))
def compute_total(orders):
    total = sum(with_tax(o) - (5 if o.get('coupon') else 0) for o in orders)
    return round(total, 2)
