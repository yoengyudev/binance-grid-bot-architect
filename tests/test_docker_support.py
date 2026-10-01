import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from database import GridDatabase
from docker_runner import main as docker_main, ready_to_start
from exchange_handler import load_config
from main import GridConfig


class DockerSupportTests(unittest.TestCase):
    def test_persistent_paths_and_startup_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            data = Path(temporary_directory)
            config = data / "config.json"
            database_path = data / "grid_bot.sqlite3"
            secrets = data / ".env"
            secrets.write_text("placeholder=1\n", encoding="utf-8")
            config.write_text(json.dumps({
                "exchange": {"name": "binance", "market_type": "spot", "sandbox": True},
                "grid": {
                    "symbol": "BTC/USDT", "investment_quote": 1000,
                    "lower_price": 80000, "upper_price": 100000,
                    "spacing_percent": 1.5, "stop_loss_price": 70000,
                    "poll_seconds": 10,
                },
            }), encoding="utf-8")
            environment = {
                "GRID_BOT_CONFIG_PATH": str(config),
                "GRID_BOT_DB_PATH": str(database_path),
            }
            with patch.dict("os.environ", environment):
                self.assertFalse(ready_to_start(secrets))
                self.assertFalse(database_path.exists())
                database_path.write_text("not a SQLite database", encoding="utf-8")
                self.assertFalse(ready_to_start(secrets))
                database_path.unlink()
                GridDatabase().set_state("grid_run", "existing state")
                self.assertTrue(ready_to_start(secrets))
                self.assertEqual(load_config()["grid"]["symbol"], "BTC/USDT")
                self.assertEqual(GridConfig.load().lower_price, 80000)
                self.assertEqual(GridDatabase().get_state("grid_run"), "existing state")

    def test_safety_halt_is_not_restarted_but_network_failure_is(self) -> None:
        with patch("docker_runner.ready_to_start", return_value=True), \
             patch("docker_runner.signal.signal"), \
             patch("docker_runner.subprocess.Popen") as popen:
            popen.return_value.wait.return_value = 2
            self.assertEqual(docker_main(), 0)
            popen.assert_called_with([sys.executable, "main.py", "--ready"])
            popen.return_value.wait.return_value = 1
            self.assertEqual(docker_main(), 1)


if __name__ == "__main__":
    unittest.main()
