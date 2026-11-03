import unittest
from messy import compute_total
class T(unittest.TestCase):
    def test_mix(self):
        o=[{'region':'EU','amount':100},{'region':'US','amount':100,'coupon':True},{'region':'XX','amount':10}]
        self.assertEqual(compute_total(o), 232.0)
