import unittest

from polytrade_esports.polymarket import PolymarketBookClient


class PolymarketTests(unittest.TestCase):
    def test_best_levels_do_not_trust_input_order(self):
        book = {
            "bids": [{"price": "0.40"}, {"price": "0.44"}],
            "asks": [{"price": "0.50"}, {"price": "0.46"}],
        }
        self.assertEqual(PolymarketBookClient._best(book, "bids"), 0.44)
        self.assertEqual(PolymarketBookClient._best(book, "asks"), 0.46)


if __name__ == "__main__":
    unittest.main()

