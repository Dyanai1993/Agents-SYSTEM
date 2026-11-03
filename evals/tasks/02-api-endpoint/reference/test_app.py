import unittest
import app
class T(unittest.TestCase):
    def test_post(self):
        self.assertEqual(app.handle('POST','/items',{'name':'a'})[0],201)
