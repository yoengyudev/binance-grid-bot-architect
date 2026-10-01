import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import Mock, patch

from database import GridDatabase, resolve_database_path
from exchange_handler import create_exchange, load_config
from trading_environment import BASE_DIR, TradingEnvironment, get_trading_settings


class TradingEnvironmentTests(unittest.TestCase):
    def setUp(self):
        self.process_environment = dict(os.environ)
        self.env = patch.dict(os.environ, {
            "TRADING_ENVIRONMENT": "TESTNET",
            "BINANCE_TESTNET_API_KEY": "test-key",
            "BINANCE_TESTNET_API_SECRET": "test-secret",
            "BINANCE_MAINNET_API_KEY": "live-key",
            "BINANCE_MAINNET_API_SECRET": "live-secret",
        }, clear=True)
        self.env.start()
        self.dotenv = patch("trading_environment.load_dotenv")
        self.dotenv.start()
        get_trading_settings.cache_clear()

    def tearDown(self):
        get_trading_settings.cache_clear()
        self.dotenv.stop()
        self.env.stop()

    def select(self, environment):
        os.environ["TRADING_ENVIRONMENT"] = environment
        get_trading_settings.cache_clear()

    def test_environment_has_no_default_or_normalization(self):
        for value in (None, "", "testnet", "MAINNET ", "production"):
            with self.subTest(value=value):
                if value is None:
                    os.environ.pop("TRADING_ENVIRONMENT", None)
                    get_trading_settings.cache_clear()
                else:
                    self.select(value)
                with self.assertRaisesRegex(ValueError, "TRADING_ENVIRONMENT"):
                    create_exchange()

    def test_selected_credentials_required_without_cross_environment_fallback(self):
        for environment in TradingEnvironment:
            for suffix in ("API_KEY", "API_SECRET"):
                with self.subTest(environment=environment, missing=suffix):
                    self.select(environment.value)
                    name = f"BINANCE_{environment.value}_{suffix}"
                    original = os.environ[name]
                    os.environ[name] = "  "
                    with patch("exchange_handler.ccxt.binance") as factory:
                        with self.assertRaisesRegex(ValueError, name):
                            create_exchange()
                        factory.assert_not_called()
                    os.environ[name] = original

    def test_selected_keys_and_sandbox_are_first_client_call(self):
        for environment, key, secret, sandbox in (
            ("TESTNET", "test-key", "test-secret", True),
            ("MAINNET", "live-key", "live-secret", False),
        ):
            self.select(environment)
            client = Mock()
            with patch("exchange_handler.ccxt.binance", return_value=client) as factory:
                self.assertIs(create_exchange(), client)
                options = factory.call_args.args[0]
                self.assertEqual((options["apiKey"], options["secret"]), (key, secret))
                self.assertEqual(options["options"]["defaultType"], "spot")
                self.assertEqual(client.mock_calls, [unittest.mock.call.set_sandbox_mode(sandbox)])
            # No requests: inspect the actual CCXT URL routing as well.
            real_client = create_exchange()
            expected_host = "testnet.binance.vision" if sandbox else "api.binance.com"
            self.assertIn(expected_host, real_client.urls["api"]["public"])
            self.assertEqual(load_config()["exchange"]["sandbox"], sandbox)

    def test_process_selection_is_frozen_and_secrets_are_not_in_repr(self):
        first = get_trading_settings()
        os.environ["TRADING_ENVIRONMENT"] = "MAINNET"
        self.assertIs(get_trading_settings(), first)
        self.assertNotIn("test-key", repr(first))
        self.assertNotIn("test-secret", repr(first))

    def test_database_isolation_and_copied_database_rejection(self):
        with tempfile.TemporaryDirectory() as directory:
            os.environ["GRID_BOT_DB_DIR"] = directory
            test_db = GridDatabase()
            test_db.set_state("grid_run", "test-only-state")
            self.select("MAINNET")
            live_db = GridDatabase()
            self.assertEqual(live_db.path.name, "grid_mainnet.sqlite")
            self.assertIsNone(live_db.get_state("grid_run"))
            live_db.set_state("grid_run", "live-only-state")
            self.select("TESTNET")
            self.assertEqual(GridDatabase().get_state("grid_run"), "test-only-state")
            copied = Path(directory) / "copy" / "grid_mainnet.sqlite"
            copied.parent.mkdir()
            shutil.copyfile(test_db.path, copied)
            self.select("MAINNET")
            with self.assertRaisesRegex(ValueError, "different trading environment"):
                GridDatabase(copied)
            with closing(sqlite3.connect(copied)) as connection:
                self.assertEqual(connection.execute(
                    "SELECT environment FROM trading_environment"
                ).fetchone()[0], "TESTNET")

    def test_database_overrides_and_unbound_legacy_state_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            for filename in ("grid_bot.sqlite3", "grid_mainnet.sqlite", "arbitrary.sqlite"):
                path = Path(directory) / filename
                with self.assertRaisesRegex(ValueError, "filename"):
                    GridDatabase(path)
                self.assertFalse(path.exists())
                os.environ["GRID_BOT_DB_PATH"] = str(path)
                with self.assertRaisesRegex(ValueError, "filename"):
                    resolve_database_path()
            os.environ.pop("GRID_BOT_DB_PATH")
            legacy = Path(directory) / "grid_testnet.sqlite"
            with closing(sqlite3.connect(legacy)) as connection, connection:
                connection.execute("CREATE TABLE bot_state (key TEXT, value TEXT)")
                connection.execute("INSERT INTO bot_state VALUES ('grid_run', 'legacy')")
            with self.assertRaisesRegex(ValueError, "unbound legacy"):
                GridDatabase(legacy)

    def test_asgi_import_and_monitor_start_fail_before_serving(self):
        code = (
            "from unittest.mock import patch; "
            "guard = patch('dotenv.load_dotenv'); guard.start(); import main"
        )
        cases = (
            ({"TRADING_ENVIRONMENT": None}, "TRADING_ENVIRONMENT"),
            ({"TRADING_ENVIRONMENT": ""}, "TRADING_ENVIRONMENT"),
            ({"TRADING_ENVIRONMENT": "invalid"}, "TRADING_ENVIRONMENT"),
            ({"BINANCE_TESTNET_API_KEY": ""}, "BINANCE_TESTNET_API_KEY"),
            ({"TRADING_ENVIRONMENT": "MAINNET", "BINANCE_MAINNET_API_SECRET": ""},
             "BINANCE_MAINNET_API_SECRET"),
        )
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                process_env = {**self.process_environment, **os.environ, **overrides}
                process_env = {name: value for name, value in process_env.items() if value is not None}
                result = subprocess.run(
                    [sys.executable, "-c", code, "--monitor-only"],
                    cwd=BASE_DIR, env=process_env, capture_output=True, text=True, timeout=30,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)

    def test_status_reports_selected_environment_without_opening_a_database(self):
        code = (
            "from unittest.mock import patch; "
            "guard = patch('dotenv.load_dotenv'); guard.start(); "
            "import main; main.app.state.monitor_only = True; "
            "assert main.bot_status()['trading_environment'] == "
            "main.TRADING_SETTINGS.environment.value"
        )
        for environment in TradingEnvironment:
            with self.subTest(environment=environment):
                process_env = {**self.process_environment, **os.environ,
                               "TRADING_ENVIRONMENT": environment.value}
                result = subprocess.run(
                    [sys.executable, "-c", code], cwd=BASE_DIR, env=process_env,
                    capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
