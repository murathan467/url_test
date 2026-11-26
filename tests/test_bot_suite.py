import asyncio
import os
import tempfile
import unittest
from unittest.mock import patch

from core.browser import _load_valid_storage_state, _mask_proxy_server, get_proxy_config
from core.notifier import StockWaveNotifier, _split_message
from core.session import (
    clear_session_valid_marker,
    has_valid_session_marker,
    mark_session_valid,
)
from mango.mango_bot import _has_mango_api_auth_failure, _norm_fold as mango_norm
from oxxo.oxxo_bot import _norm_fold as oxxo_norm
from zara.zara_bot import _ZaraNetworkMonitor, _normalize_tr_lower


class TestCoreBrowser(unittest.TestCase):
    def test_get_proxy_config_parses_and_unquotes(self):
        with patch.dict(
            os.environ,
            {"HOME_PROXY_URL": "http://user%401:p%40ss%3Aword@100.64.0.10:8888"},
            clear=False,
        ):
            cfg = get_proxy_config()
            self.assertEqual(
                cfg,
                {
                    "server": "http://100.64.0.10:8888",
                    "username": "user@1",
                    "password": "p@ss:word",
                },
            )

    def test_get_proxy_config_without_port_and_empty(self):
        with patch.dict(os.environ, {"HOME_PROXY_URL": "http://proxy.example.ts.net"}, clear=False):
            self.assertEqual(get_proxy_config(), {"server": "http://proxy.example.ts.net"})
        with patch.dict(os.environ, {"HOME_PROXY_URL": ""}, clear=False):
            self.assertIsNone(get_proxy_config())
        with patch.dict(os.environ, {"HOME_PROXY_URL": "not-a-url"}, clear=False):
            self.assertIsNone(get_proxy_config())

    def test_mask_proxy_server_hides_middle_octets(self):
        masked = _mask_proxy_server("http://100.106.95.97:8888")
        self.assertEqual(masked, "http://100.x.x.97:8888")
        self.assertNotIn("106", masked)
        self.assertNotIn("95", masked)

    def test_load_valid_storage_state(self):
        with tempfile.TemporaryDirectory() as td:
            valid = os.path.join(td, "ok.json")
            with open(valid, "w", encoding="utf-8") as f:
                f.write('{"cookies": [], "origins": []}')
            self.assertEqual(_load_valid_storage_state(valid), valid)

            bad = os.path.join(td, "bad.json")
            with open(bad, "w", encoding="utf-8") as f:
                f.write("not json")
            self.assertIsNone(_load_valid_storage_state(bad))


class TestNotifier(unittest.TestCase):
    def test_clean_url_preserves_color_variant_and_strips_tracking(self):
        u1 = StockWaveNotifier._clean_url(
            "https://shop.mango.com/tr/tr/p/elbise_123?c=99&utm_source=ig"
        )
        u2 = StockWaveNotifier._clean_url(
            "https://shop.mango.com/tr/tr/p/elbise_123?c=02&fbclid=xyz"
        )
        self.assertNotEqual(u1, u2)
        self.assertEqual(u1, "https://shop.mango.com/tr/tr/p/elbise_123?c=99")
        self.assertEqual(u2, "https://shop.mango.com/tr/tr/p/elbise_123?c=02")

    @patch("core.notifier.send_telegram_message")
    def test_wave_limit_and_uncertain_url_guard(self, mock_send):
        notifier = StockWaveNotifier("MANGO")
        prod = {
            "name": "Elbise",
            "url": "https://shop.mango.com/tr/tr/p/elbise_1?c=99",
            "oldPrice": "1500 TL",
            "newPrice": "999 TL",
            "available_sizes": ["S"],
        }
        # 3 wave alerts, 4th silent
        for _ in range(4):
            notifier.process_cycle([prod])
        self.assertEqual(mock_send.call_count, 3)

        # Transient tab error must NOT send out-of-stock or reset wave counter
        notifier.process_cycle([], uncertain_urls={prod["url"]})
        self.assertEqual(mock_send.call_count, 3)
        notifier.process_cycle([prod])
        self.assertEqual(mock_send.call_count, 3)

        # Genuine out-of-stock sends reset alert and restarts wave
        notifier.process_cycle([])
        self.assertEqual(mock_send.call_count, 4)
        notifier.process_cycle([prod])
        self.assertEqual(mock_send.call_count, 5)

    def test_split_message(self):
        chunks = _split_message("A" * 2500 + "\n\n" + "B" * 2500, limit=3000)
        self.assertEqual(len(chunks), 2)


class TestSessionMarkers(unittest.TestCase):
    def test_marker_lifecycle(self):
        store = "_unit_test_store"
        clear_session_valid_marker(store)
        self.assertFalse(has_valid_session_marker(store))
        mark_session_valid(store)
        self.assertTrue(has_valid_session_marker(store))
        clear_session_valid_marker(store)
        self.assertFalse(has_valid_session_marker(store))


class TestStoreLogic(unittest.TestCase):
    def test_turkish_normalization(self):
        self.assertIn("bir sorun oluştu", _normalize_tr_lower("BİR SORUN OLUŞTU"))
        self.assertIn("giriş yap", _normalize_tr_lower("GİRİŞ YAP"))
        self.assertEqual(mango_norm("GİRİŞ YAP"), "giriş yap")
        self.assertEqual(mango_norm("İlk Fiyat"), "ilk fiyat")
        self.assertEqual(oxxo_norm("TÜKENDİ"), "tükendi")

    def test_mango_api_auth_failure_detection(self):
        self.assertTrue(
            _has_mango_api_auth_failure(
                ["401 https://shop.mango.com/api/favorites/list ct=application/json len=12"]
            )
        )
        self.assertFalse(
            _has_mango_api_auth_failure(
                ["200 https://shop.mango.com/api/favorites/list ct=application/json len=500"]
            )
        )

    def test_zara_network_monitor_catches_auth_after_35_responses(self):
        class FakePage:
            def __init__(self):
                self.cb = None

            def on(self, event, cb):
                self.cb = cb

            def remove_listener(self, event, cb):
                self.cb = None

        class FakeResp:
            def __init__(self, url, status, ct="application/json"):
                self.url = url
                self.status = status
                self.headers = {"content-type": ct, "content-length": "10"}

            async def text(self):
                return "{}"

        async def run():
            page = FakePage()
            mon = _ZaraNetworkMonitor(page, capture_details=True)
            for i in range(40):
                await mon._on_response(FakeResp(f"https://www.zara.com/api/item/{i}", 200))
            await mon._on_response(
                FakeResp("https://www.zara.com/tr/tr/user/oauth2/refresh", 400)
            )
            await mon._on_response(
                FakeResp("https://www.zara.com/tr/tr/user/wishlists/item-ids", 403)
            )
            self.assertTrue(mon.has_auth_failure())
            self.assertEqual(len(mon.auth_failures), 2)
            mon.stop()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
