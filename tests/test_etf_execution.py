import unittest
from unittest.mock import patch

from test_etf import securities

from ritcx.etf.bot import ETFClient, validate
from ritcx.runtime import TradingError


class ETFExecutionTests(unittest.TestCase):
    def client(self, execute=True):
        return ETFClient(
            "http://127.0.0.1/v1", "test", "secret", execute, audit=lambda *a, **k: None
        )

    def test_currency_orders_have_different_size_limit(self):
        client = self.client()
        # Only the transport is replaced; size validation and fill checks remain real.
        response = {"order_id": 1, "quantity_filled": 250000, "status": "TRANSACTED", "vwap": 1.0}
        before = securities()
        after = securities()
        after[1]["position"] = -250000
        with (
            patch.object(client, "request", return_value=response),
            patch.object(client, "get", side_effect=[before, after, []]),
        ):
            result = client.market("USD", "SELL", 250000)
        self.assertEqual(result["quantity_filled"], 250000)
        with self.assertRaises(TradingError):
            client.market("RITC", "SELL", 10001)

    def test_incomplete_market_fill_stops_further_trading(self):
        client = self.client()
        with (
            patch.object(client, "get", return_value=securities()),
            patch.object(client, "request", return_value={"quantity_filled": 9, "status": "OPEN"}),
        ):
            with self.assertRaises(TradingError):
                client.market("RITC", "BUY", 10)

    def test_dry_run_and_unknown_ticker_cannot_trade(self):
        with self.assertRaises(TradingError):
            self.client(False).market("RITC", "BUY", 1)
        with self.assertRaises(TradingError):
            self.client().market("UNKNOWN", "BUY", 1)

    def test_changed_fx_fees_contract_currency_and_cash_limits_are_rejected(self):
        case = dict(
            name="RITCx - Algorithmic ETF Arbitrage Case", ticks_per_period=300, total_periods=1
        )
        limits = [
            dict(name="LIMIT-STOCK", gross_limit=300000, net_limit=200000),
            dict(name="LIMIT-CASH", gross_limit=10000000, net_limit=10000000),
        ]
        validate(case, securities(), limits)
        for ticker, field, value in [
            ("USD", "trading_fee", 0.01),
            ("USD", "max_trade_size", 1000),
            ("RITC", "size", 100),
            ("BULL", "currency", "USD"),
        ]:
            with self.subTest(ticker=ticker, field=field):
                sec = securities()
                next(s for s in sec if s["ticker"] == ticker)[field] = value
                with self.assertRaises(TradingError):
                    validate(case, sec, limits)
        limits[1]["gross_limit"] = 1000000
        with self.assertRaises(TradingError):
            validate(case, securities(), limits)


if __name__ == "__main__":
    unittest.main()
