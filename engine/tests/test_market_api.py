import unittest

import httpx

from quantdesk.api.server import app


class MarketGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []

        def upstream(request):
            self.calls.append(request)
            return httpx.Response(200, json={"retCode": 0, "result": {"list": []}})

        self.upstream = httpx.AsyncClient(transport=httpx.MockTransport(upstream))
        app.state.client = self.upstream
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")

    async def asyncTearDown(self):
        await self.client.aclose()
        await self.upstream.aclose()

    async def test_symbol_mapping(self):
        response = await self.client.get("/bybit/v5/market/tickers", params={"symbol": "AMD"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.calls[0].url.params["symbol"], "AMDSTOCKUSDT")
        self.assertEqual(self.calls[0].url.params["category"], "linear")

    async def test_invalid_queries_never_reach_upstream(self):
        for params in ({"symbol": "DOGEUSDT"}, {"symbol": "BTCUSDT", "category": "spot"}, {"symbol": "BTCUSDT", "interval": "1"}, {"symbol": "BTCUSDT", "limit": 1001}):
            response = await self.client.get("/bybit/v5/market/kline", params=params)
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.calls, [])

    async def test_timeout_is_bounded_error(self):
        def timeout(request):
            raise httpx.ReadTimeout("private upstream details", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(timeout)) as upstream:
            app.state.client = upstream
            response = await self.client.get("/bybit/v5/market/tickers", params={"symbol": "BTCUSDT"})
        self.assertEqual(response.status_code, 504)
        self.assertNotIn("private upstream details", response.text)

    async def test_exchange_failure_is_not_success(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"retCode": 10006}))) as upstream:
            app.state.client = upstream
            response = await self.client.get("/bybit/v5/market/tickers", params={"symbol": "BTCUSDT"})
        self.assertEqual(response.status_code, 502)


if __name__ == "__main__":
    unittest.main()
