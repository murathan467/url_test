import asyncio
import io
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch

from config import SCRIPT_DIR
from core.browser import (
    _load_valid_storage_state,
    _mask_proxy_server,
    get_proxy_config,
    launch_store_browser,
)
from core.notifier import StockWaveNotifier, _split_message
from core.session import (
    clear_session_valid_marker,
    has_valid_session_marker,
    mark_session_valid,
    save_store_session,
)
from export_session import _verify_favorites_page
from main_bot import _run_store
from mango.mango_bot import _has_mango_api_auth_failure, _norm_fold as mango_norm
from oxxo.oxxo_bot import _norm_fold as oxxo_norm
from zara.zara_bot import _ZaraNetworkMonitor, _normalize_tr_lower


def _extract_workflow_script(workflow_rel_path: str) -> str:
    """Extract the JS `script: |` block from an actions/github-script workflow file."""
    path = os.path.join(SCRIPT_DIR, workflow_rel_path)
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    script_lines: list[str] = []
    in_script = False
    base_indent: int | None = None
    for line in lines:
        if not in_script:
            if re.match(r"^\s*script:\s*\|\s*$", line):
                in_script = True
            continue
        if line.strip() == "":
            script_lines.append("")
            continue
        indent = len(line) - len(line.lstrip(" "))
        if base_indent is None:
            base_indent = indent
        if indent < base_indent:
            break
        script_lines.append(line[base_indent:].rstrip("\r\n"))
    return "\n".join(script_lines)


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

    def test_get_proxy_config_rejects_invalid_scheme_and_malformed_port(self):
        for bad_url in (
            "ftp://100.64.0.10:8888",
            "http://100.64.0.10:notaport",
            "http://100.64.0.10:99999",
            "http://:8888",
        ):
            with patch.dict(os.environ, {"HOME_PROXY_URL": bad_url}, clear=False):
                self.assertIsNone(get_proxy_config(), msg=f"Should reject {bad_url}")

        with patch.dict(
            os.environ,
            {"HOME_PROXY_URL": "socks5://[fd7a:115c:a1e0::1]:1080"},
            clear=False,
        ):
            self.assertEqual(
                get_proxy_config(),
                {"server": "socks5://[fd7a:115c:a1e0::1]:1080"},
            )

    def test_mask_proxy_server_hides_middle_octets(self):
        masked = _mask_proxy_server("http://100.106.95.97:8888")
        self.assertEqual(masked, "http://100.x.x.97:8888")
        self.assertNotIn("106", masked)
        self.assertNotIn("95", masked)

        # Credentials, MagicDNS hostnames, IPv6, and malformed ports must never leak
        masked_cred = _mask_proxy_server("http://secretuser:secretpass@100.106.95.97:8888")
        self.assertNotIn("secretuser", masked_cred)
        self.assertNotIn("secretpass", masked_cred)
        self.assertEqual(masked_cred, "http://100.x.x.97:8888")

        masked_host = _mask_proxy_server("http://murat-pc.tail12345.ts.net:8888")
        self.assertEqual(masked_host, "http://mu***:8888")
        self.assertNotIn("murat", masked_host)
        self.assertNotIn("tail12345", masked_host)

        masked_v6 = _mask_proxy_server("http://[fd7a:115c:a1e0::1]:8888")
        self.assertEqual(masked_v6, "http://[fd7a::***]:8888")
        self.assertNotIn("115c", masked_v6)

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

            empty = os.path.join(td, "empty.json")
            with open(empty, "w", encoding="utf-8") as f:
                f.write("")
            self.assertIsNone(_load_valid_storage_state(empty))

            wrong_shape = os.path.join(td, "wrong_shape.json")
            with open(wrong_shape, "w", encoding="utf-8") as f:
                f.write('{"cookies": null, "origins": []}')
            self.assertIsNone(_load_valid_storage_state(wrong_shape))

            wrong_origins = os.path.join(td, "wrong_origins.json")
            with open(wrong_origins, "w", encoding="utf-8") as f:
                f.write('{"cookies": [], "origins": "bad"}')
            self.assertIsNone(_load_valid_storage_state(wrong_origins))

            self.assertIsNone(_load_valid_storage_state(os.path.join(td, "missing.json")))

    def test_launch_store_browser_no_global_route_and_masked_log(self):
        class FakeContext:
            def __init__(self):
                self.route = AsyncMock()

        class FakeBrowser:
            def __init__(self):
                self.context = FakeContext()
                self.new_context_kwargs = None

            async def new_context(self, **kwargs):
                self.new_context_kwargs = kwargs
                return self.context

        class FakeChromium:
            def __init__(self):
                self.browser = FakeBrowser()

            async def launch(self, **kwargs):
                return self.browser

        class FakePlaywright:
            def __init__(self):
                self.chromium = FakeChromium()

        async def run():
            pw = FakePlaywright()
            buf = io.StringIO()
            with patch.dict(
                os.environ,
                {"HOME_PROXY_URL": "http://gizuser:supersecret@100.106.95.97:8888"},
                clear=False,
            ), patch("core.browser._load_valid_storage_state", return_value=None):
                with redirect_stdout(buf):
                    browser, context = await launch_store_browser(pw, "mango")

            output = buf.getvalue()
            context.route.assert_not_called()
            self.assertIn("http://100.x.x.97:8888", output)
            self.assertNotIn("106", output)
            self.assertNotIn("95", output)
            self.assertNotIn("gizuser", output)
            self.assertNotIn("supersecret", output)
            self.assertEqual(
                browser.new_context_kwargs["proxy"],
                {
                    "server": "http://100.106.95.97:8888",
                    "username": "gizuser",
                    "password": "supersecret",
                },
            )

        asyncio.run(run())


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

        # Case-insensitive variant param key, fragment stripping, and Zara v1 vs v2
        z1 = StockWaveNotifier._clean_url(
            "HTTPS://WWW.ZARA.COM/tr/tr/gomlek-p01234567.html?v1=345678901&v2=2420896#top"
        )
        z2 = StockWaveNotifier._clean_url(
            "https://www.zara.com/tr/tr/gomlek-p01234567.html?V1=345678901&utm_medium=social"
        )
        self.assertEqual(z1, z2)
        self.assertEqual(z1, "https://www.zara.com/tr/tr/gomlek-p01234567.html?v1=345678901")

        # Oxxo color variant
        ox = StockWaveNotifier._clean_url(
            "https://www.oxxo.com.tr/tr/keten-pantolon?color=siyah&ref=fav"
        )
        self.assertEqual(ox, "https://www.oxxo.com.tr/tr/keten-pantolon?color=siyah")
        self.assertEqual(StockWaveNotifier._clean_url(""), "")

    @patch("core.notifier.send_telegram_message")
    def test_wave_limit_and_uncertain_url_guard(self, mock_send):
        notifier = StockWaveNotifier("MANGO")
        prod = {
            "name": "Elbise <Özel>",
            "url": "https://shop.mango.com/tr/tr/p/elbise_1?c=99",
            "oldPrice": "1500 TL",
            "newPrice": "999 TL",
            "available_sizes": ["S"],
        }
        # 3 wave alerts, 4th silent
        for _ in range(4):
            notifier.process_cycle([prod])
        self.assertEqual(mock_send.call_count, 3)
        self.assertIn("Elbise &lt;Özel&gt;", mock_send.call_args_list[0].args[0])

        # Transient tab error (even with extra tracking params) must NOT reset wave counter
        notifier.process_cycle(
            [], uncertain_urls={"https://shop.mango.com/tr/tr/p/elbise_1?c=99&utm_source=test"}
        )
        self.assertEqual(mock_send.call_count, 3)
        notifier.process_cycle([prod])
        self.assertEqual(mock_send.call_count, 3)

        # Genuine out-of-stock sends reset alert and restarts wave
        notifier.process_cycle([])
        self.assertEqual(mock_send.call_count, 4)
        self.assertIn("STOK TÜKENDİ", mock_send.call_args_list[3].args[0])
        notifier.process_cycle([prod])
        self.assertEqual(mock_send.call_count, 5)
        self.assertIn("Bildirim 1/", mock_send.call_args_list[4].args[0])

    def test_split_message(self):
        chunks = _split_message("A" * 2500 + "\n\n" + "B" * 2500, limit=3000)
        self.assertEqual(len(chunks), 2)

        # Oversized single paragraph should split cleanly on single newline boundaries
        html_lines = [f"<b>Satir {i:02d}</b>: {'X' * 40}" for i in range(10)]
        single_block = "\n".join(html_lines)
        line_chunks = _split_message(single_block, limit=160)
        self.assertGreater(len(line_chunks), 1)
        for chunk in line_chunks:
            self.assertLessEqual(len(chunk), 160)
            self.assertEqual(chunk.count("<b>"), chunk.count("</b>"))


class TestSessionAndMainBot(unittest.TestCase):
    def test_marker_lifecycle(self):
        store = "_unit_test_store"
        clear_session_valid_marker(store)
        self.assertFalse(has_valid_session_marker(store))
        mark_session_valid(store)
        self.assertTrue(has_valid_session_marker(store))
        clear_session_valid_marker(store)
        self.assertFalse(has_valid_session_marker(store))

    def test_save_store_session_marks_valid_only_on_success(self):
        store = "_unit_test_save_store"
        clear_session_valid_marker(store)

        class FakeContext:
            def __init__(self, should_fail=False):
                self.should_fail = should_fail

            async def storage_state(self, path: str):
                if self.should_fail:
                    raise RuntimeError("disk error")
                with open(path, "w", encoding="utf-8") as f:
                    f.write('{"cookies": [], "origins": []}')

        async def run():
            with tempfile.TemporaryDirectory() as td:
                state_file = os.path.join(td, f"{store}.json")
                with patch("core.session.get_storage_state_path", return_value=state_file):
                    ok_fail = await save_store_session(FakeContext(should_fail=True), store, mark_valid=True)
                    self.assertFalse(ok_fail)
                    self.assertFalse(has_valid_session_marker(store))

                    ok_no_mark = await save_store_session(FakeContext(), store, mark_valid=False)
                    self.assertTrue(ok_no_mark)
                    self.assertFalse(has_valid_session_marker(store))

                    ok_mark = await save_store_session(FakeContext(), store, mark_valid=True)
                    self.assertTrue(ok_mark)
                    self.assertTrue(has_valid_session_marker(store))

        try:
            asyncio.run(run())
        finally:
            clear_session_valid_marker(store)

    def _run_main_bot_scenario(self, store: str, loop_side_effect):
        fake_browser = AsyncMock()
        fake_context = AsyncMock()

        class FakePlaywrightCM:
            async def __aenter__(self):
                return object()

            async def __aexit__(self, exc_type, exc, tb):
                return False

        save_mock = AsyncMock(return_value=True)
        loop_target = {
            "zara": "zara.zara_bot.run_zara_loop",
            "mango": "mango.mango_bot.run_mango_loop",
            "oxxo": "oxxo.oxxo_bot.run_oxxo_loop",
        }[store]

        async def run():
            with patch("patchright.async_api.async_playwright", return_value=FakePlaywrightCM()), patch(
                "core.browser.launch_store_browser",
                new=AsyncMock(return_value=(fake_browser, fake_context)),
            ), patch(loop_target, new=AsyncMock(side_effect=loop_side_effect)), patch(
                "core.session.save_store_session", new=save_mock
            ):
                return await _run_store(store)

        return run, save_mock, fake_browser, fake_context

    def test_run_store_saves_session_when_ok_true(self):
        store = "mango"
        clear_session_valid_marker(store)
        try:
            async def healthy_loop(context, max_minutes):
                mark_session_valid(store)
                return True

            coro_fn, save_mock, browser, context = self._run_main_bot_scenario(store, healthy_loop)
            ok = asyncio.run(coro_fn())
            self.assertTrue(ok)
            save_mock.assert_awaited_once_with(context, store, mark_valid=True)
            context.close.assert_awaited_once()
            browser.close.assert_awaited_once()
        finally:
            clear_session_valid_marker(store)

    def test_run_store_prevents_cache_poisoning_when_ok_false_after_midrun_valid_marker(self):
        store = "mango"
        clear_session_valid_marker(store)
        try:
            async def midrun_outage_loop(context, max_minutes):
                # Simulates Cycle 1 saving a valid marker, then Cycle 100 ending with return False
                mark_session_valid(store)
                return False

            coro_fn, save_mock, browser, context = self._run_main_bot_scenario(
                store, midrun_outage_loop
            )
            ok = asyncio.run(coro_fn())
            self.assertFalse(ok)
            save_mock.assert_not_awaited()
            self.assertFalse(has_valid_session_marker(store))
            context.close.assert_awaited_once()
            browser.close.assert_awaited_once()
        finally:
            clear_session_valid_marker(store)

    def test_run_store_clears_marker_and_closes_browser_on_unhandled_exception(self):
        store = "zara"
        clear_session_valid_marker(store)
        try:
            async def crashing_loop(context, max_minutes):
                mark_session_valid(store)
                raise RuntimeError("unexpected crash")

            coro_fn, save_mock, browser, context = self._run_main_bot_scenario(store, crashing_loop)
            with self.assertRaises(RuntimeError):
                asyncio.run(coro_fn())
            save_mock.assert_not_awaited()
            self.assertFalse(has_valid_session_marker(store))
            context.close.assert_awaited_once()
            browser.close.assert_awaited_once()
        finally:
            clear_session_valid_marker(store)


class TestExportSession(unittest.TestCase):
    def test_verify_favorites_page_waits_past_transient_ssr_login_cta(self):
        class FakePage:
            def __init__(self):
                self.url = "https://www.zara.com/tr/tr/user/wishlist"
                self.calls = 0

            def evaluate(self, script):
                self.calls += 1
                if self.calls <= 2:
                    return {"prodCount": 0, "hasLoginCta": True, "hasLoginForm": False}
                return {"prodCount": 4, "hasLoginCta": False, "hasLoginForm": False}

            def wait_for_timeout(self, ms):
                pass

        clock = {"t": 0.0}

        def fake_mono():
            clock["t"] += 0.5
            return clock["t"]

        with patch("export_session.time.monotonic", side_effect=fake_mono):
            verified, detail = _verify_favorites_page(
                FakePage(), "zara", timeout_sec=10.0, login_cta_grace_sec=4.5
            )
        self.assertTrue(verified)
        self.assertIn("urun_link=4", detail)

    def test_verify_favorites_page_fails_on_persistent_login_cta(self):
        class FakePage:
            url = "https://www.zara.com/tr/tr/user/wishlist"

            def evaluate(self, script):
                return {"prodCount": 0, "hasLoginCta": True, "hasLoginForm": False}

            def wait_for_timeout(self, ms):
                pass

        clock = {"t": 0.0}

        def fake_mono():
            clock["t"] += 1.0
            return clock["t"]

        with patch("export_session.time.monotonic", side_effect=fake_mono):
            verified, detail = _verify_favorites_page(
                FakePage(), "zara", timeout_sec=6.0, login_cta_grace_sec=3.0
            )
        self.assertFalse(verified)
        self.assertIn("giriş çağrısı", detail)

    def test_verify_favorites_page_fails_on_login_redirect_and_password_form(self):
        class RedirectPage:
            url = "https://account.zara.com/init-authorize"

            def evaluate(self, script):
                raise AssertionError("Should detect URL redirect first")

            def wait_for_timeout(self, ms):
                pass

        verified, detail = _verify_favorites_page(RedirectPage(), "zara", timeout_sec=5.0)
        self.assertFalse(verified)
        self.assertIn("yönlendirildi", detail)

        class FormPage:
            url = "https://shop.mango.com/tr/tr/favorites"

            def evaluate(self, script):
                return {"prodCount": 0, "hasLoginCta": True, "hasLoginForm": True}

            def wait_for_timeout(self, ms):
                pass

        verified2, detail2 = _verify_favorites_page(FormPage(), "mango", timeout_sec=5.0)
        self.assertFalse(verified2)
        self.assertIn("giriş formu", detail2)


class TestWorkflowChainingLogic(unittest.TestCase):
    def test_tracker_and_watchdog_static_invariants(self):
        tracker_path = os.path.join(SCRIPT_DIR, ".github", "workflows", "tracker.yml")
        watchdog_path = os.path.join(SCRIPT_DIR, ".github", "workflows", "watchdog.yml")
        with open(tracker_path, "r", encoding="utf-8") as f:
            tracker = f.read()
        with open(watchdog_path, "r", encoding="utf-8") as f:
            watchdog = f.read()

        self.assertIn("fail-fast: false", tracker)
        self.assertIn('echo "::add-mask::$EXIT_IP"', tracker)
        self.assertIn('[ -f ".session_cache/${{ matrix.store }}.valid" ]', tracker)
        self.assertIn('$2="[host]"; $3="[user]"', tracker)
        self.assertIn('if (trackResult !== "success" && elapsedSec < 600)', tracker)
        self.assertIn('retriggerJob.conclusion === "skipped"', watchdog)

    @unittest.skipUnless(shutil.which("node"), "Node.js required to simulate workflow scripts")
    def test_retrigger_and_watchdog_js_simulation(self):
        retrigger_js = _extract_workflow_script(os.path.join(".github", "workflows", "tracker.yml"))
        watchdog_js = _extract_workflow_script(os.path.join(".github", "workflows", "watchdog.yml"))
        self.assertTrue(retrigger_js.strip())
        self.assertTrue(watchdog_js.strip())

        # Replace GitHub Actions template placeholders with test JS variables
        retrigger_exec = (
            retrigger_js.replace("${{ needs.track-store.result }}", "failure")
            .replace("${{ inputs.chain_depth || '1' }}", "5")
            .replace("${{ github.ref_name }}", "main")
        )

        harness = f"""
        const assert = require("assert");

        async function runRetrigger(elapsedSeconds) {{
          const dispatched = [];
          const startedIso = new Date(Date.now() - elapsedSeconds * 1000).toISOString();
          const context = {{ repo: {{ owner: "o", repo: "r" }}, runId: 999 }};
          const github = {{
            rest: {{
              actions: {{
                getWorkflowRun: async () => ({{ data: {{ run_started_at: startedIso }} }}),
                listWorkflowRuns: async () => ({{ data: {{ workflow_runs: [{{ id: 999, status: "in_progress" }}] }} }}),
                createWorkflowDispatch: async (args) => {{ dispatched.push(args); }},
              }},
            }},
          }};
          const fn = new Function("github", "context", `return (async () => {{ ${{ {retrigger_exec!r} }} }})();`);
          await fn(github, context);
          return dispatched;
        }}

        async function runWatchdog(runs, jobsByRunId) {{
          const dispatched = [];
          const context = {{ repo: {{ owner: "o", repo: "r" }} }};
          const github = {{
            rest: {{
              actions: {{
                listWorkflowRuns: async () => ({{ data: {{ workflow_runs: runs }} }}),
                listJobsForWorkflowRun: async ({{ run_id }}) => ({{ data: {{ jobs: jobsByRunId[run_id] || [] }} }}),
                createWorkflowDispatch: async (args) => {{ dispatched.push(args); }},
              }},
            }},
          }};
          const fn = new Function("github", "context", `return (async () => {{ ${{ {watchdog_js!r} }} }})();`);
          await fn(github, context);
          return dispatched;
        }}

        (async () => {{
          // 1) Zara failed in 65s, Mango & Oxxo ran 5.5h -> elapsedSec=20100 >= 600 -> retrigger MUST dispatch!
          const d1 = await runRetrigger(20100);
          assert.strictEqual(d1.length, 1, "Long partial-failure run must retrigger");
          assert.strictEqual(d1[0].inputs.chain_depth, "6");

          // 2) All 3 stores crashed in 65s -> elapsedSec=65 < 600 -> retrigger MUST skip!
          const d2 = await runRetrigger(65);
          assert.strictEqual(d2.length, 0, "Short total-failure run must not instant-retrigger");

          const now = Date.now();
          const mkRun = (id, durSec, conclusion = "failure") => ({{
            id,
            status: "completed",
            conclusion,
            run_started_at: new Date(now - (durSec + 60) * 1000).toISOString(),
            updated_at: new Date(now - 60 * 1000).toISOString(),
          }});

          // 3) 3 short manual single-store Zara failures (retrigger skipped) + older 5.5h run -> Watchdog MUST recover!
          const manualRuns = [mkRun(1, 70), mkRun(2, 75), mkRun(3, 65), mkRun(4, 20000)];
          const manualJobs = {{
            1: [{{ name: "Sonraki 7/24 Turu Baslat", conclusion: "skipped" }}],
            2: [{{ name: "Sonraki 7/24 Turu Baslat", conclusion: "skipped" }}],
            3: [{{ name: "Sonraki 7/24 Turu Baslat", conclusion: "skipped" }}],
          }};
          const w1 = await runWatchdog(manualRuns, manualJobs);
          assert.strictEqual(w1.length, 1, "Single-store test failures must not trip 24/7 circuit breaker");

          // 4) 3 short full-chain failures (retrigger ran) -> Watchdog MUST trip circuit breaker!
          const fullFailRuns = [mkRun(10, 70), mkRun(11, 75), mkRun(12, 65)];
          const fullFailJobs = {{
            10: [{{ name: "Sonraki 7/24 Turu Baslat", conclusion: "success" }}],
            11: [{{ name: "Sonraki 7/24 Turu Baslat", conclusion: "success" }}],
            12: [{{ name: "Sonraki 7/24 Turu Baslat", conclusion: "success" }}],
          }};
          const w2 = await runWatchdog(fullFailRuns, fullFailJobs);
          assert.strictEqual(w2.length, 0, "3 consecutive short full-chain failures must trip circuit breaker");

          // 5) 3 long 5.5h partial failures (Zara fail, Mango/Oxxo ok) -> Watchdog MUST NOT trip breaker!
          const longPartialRuns = [mkRun(20, 20100), mkRun(21, 20200), mkRun(22, 20050)];
          const w3 = await runWatchdog(longPartialRuns, {{}});
          assert.strictEqual(w3.length, 1, "5.5h partial failures must not trip circuit breaker");

          console.log("ALL_WORKFLOW_SIMULATIONS_OK");
        }})().catch((e) => {{
          console.error(e);
          process.exit(1);
        }});
        """

        res = subprocess.run(
            ["node", "-e", harness],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(
            res.returncode,
            0,
            msg=f"Workflow JS simulation failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )
        self.assertIn("ALL_WORKFLOW_SIMULATIONS_OK", res.stdout)


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
