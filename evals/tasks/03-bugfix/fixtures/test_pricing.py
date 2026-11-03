import unittest
from pricing import discount
class T(unittest.TestCase):
    def test_ten_percent(self):
        self.assertAlmostEqual(discount(200, 10), 180)
    def test_zero(self):
        self.assertEqual(discount(50, 0), 50)
