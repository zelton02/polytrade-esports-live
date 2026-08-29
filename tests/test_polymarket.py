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

    def test_pair_preserves_full_depth_and_maps_books_by_asset_id(self):
        client = PolymarketBookClient()
        client.get_books = lambda tokens: [
            {
                "asset_id": "token-b", "timestamp": "1787990400000",
                "bids": [{"price": "0.51", "size": "8"}],
                "asks": [{"price": "0.53", "size": "9"}],
            },
            {
                "asset_id": "token-a", "timestamp": "1787990400000",
                "bids": [
                    {"price": "0.45", "size": "2"},
                    {"price": "0.47", "size": "3"},
                ],
                "asks": [
                    {"price": "0.52", "size": "4"},
                    {"price": "0.50", "size": "5"},
                ],
            },
        ]
        quote = client.get_pair("m1", "token-a", "token-b")
        self.assertEqual((quote.bid_a, quote.ask_a), (0.47, 0.50))
        self.assertEqual([level.price for level in quote.levels("A", "bids")], [0.47, 0.45])
        self.assertEqual([level.size for level in quote.levels("A", "asks")], [5.0, 4.0])
        self.assertEqual(quote.raw["B"]["asset_id"], "token-b")


if __name__ == "__main__":
    unittest.main()
