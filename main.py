"""Application entry point and API, runner, and notification orchestration."""

import argparse
import asyncio
import logging
import os
import sqlite3
import sys
import uuid
from decimal import Decimal
from typing import Optional
import ccxt
import uvicorn
from api import app
from database import GridDatabase
from exchange_handler import create_exchange, create_macro_exchange, exchange_retry_delay, network_backoff
from notifier import TelegramNotifier, bind_notification_loop, close_notifications, schedule_telegram_alert
from settings import (
    ENGINE_FAULT_KEY,
    ENGINE_HALTED,
    ENGINE_IDLE,
    ENGINE_RUNNING,
    ENGINE_STATUS_KEY,
    ENGINE_STOP_CLEANUP_KEY,
    LIQUIDATION_KEY,
    RUNNER_LEASE_RENEW_SECONDS,
    RUNNER_LEASE_TTL_SECONDS,
    SAFETY_MODE_KEY,
    TRADING_SETTINGS,
    TRAILING_STOP_KEY,
    _credentials,
)
from models import (
    GridConfig,
    RunnerLeaseLost,
    TradingHalt,
)
from market_data import (
    _refresh_market_data,
)
from engine import (
    GridBot,
    _apply_active_grid_config,
    _center_config_at_current_price,
    _ensure_fault_cleanup,
    _reconcile_service_boot,
)

LOGGER = logging.getLogger(__name__)


async def _runner_heartbeat(bot: GridBot) -> None:
    while True:
        await asyncio.sleep(RUNNER_LEASE_RENEW_SECONDS)
        try:
            if await asyncio.to_thread(bot.renew_runner_lease):
                continue
        except Exception:
            LOGGER.exception("Trading runner lease renewal failed.")
        bot._runner_lease_lost = True
        bot.stop_controller.stop_requested.set()
        LOGGER.critical("Trading runner lease lost; this process is fenced out.")
        return


async def _run_services(bot: GridBot, notifier: Optional[TelegramNotifier],
                        *, ready: bool = False) -> None:
    """Own the SQLite runner fence throughout API and trading service lifetime."""
    bot.acquire_runner_lease()
    bind_notification_loop()
    schedule_telegram_alert("*Process START / RESTART*\nTrading service initialized")
    heartbeat = asyncio.create_task(_runner_heartbeat(bot), name="runner-lease")
    try:
        await _run_services_owned(bot, notifier, ready=ready)
        if bot._runner_lease_lost:
            raise RunnerLeaseLost("Trading runner lease was lost.")
    except Exception as error:
        if not getattr(error, "_telegram_alert_scheduled", False):
            await TelegramNotifier().notify_critical_error(error)
        raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        bot.release_runner_lease()
        await close_notifications()


async def _run_services_owned(bot: GridBot, notifier: Optional[TelegramNotifier],
                              *, ready: bool = False) -> None:
    """Run the local API and trading loop with outbound alerts only."""
    await _reconcile_service_boot(bot, ready=ready)
    if notifier is None:
        notifier = TelegramNotifier()
    app.state.grid_bot = bot
    app.state.ready = ready
    app.state.standby = ready and bot.database.get_state("grid_run") is None
    app.state.atr_snapshot = None
    app.state.order_book_snapshot = None
    server = uvicorn.Server(uvicorn.Config(
        app, host=os.getenv("BOT_API_HOST", "127.0.0.1"),
        port=8000, log_level="warning",
    ))

    async def serve_api() -> None:
        try:
            await server.serve()
        except SystemExit as error:
            # Uvicorn exits this way when its listening port is unavailable.
            LOGGER.error("Status API could not start (exit %s).", error.code)
        except Exception as error:
            LOGGER.error("Status API stopped: %s", type(error).__name__)
        else:
            if not server.should_exit:
                LOGGER.error("Status API stopped while the trading bot is running.")

    api_task = asyncio.create_task(serve_api(), name="status-api")
    async def stop_if_api_exits() -> None:
        await api_task
        if ready and not server.should_exit:
            bot.database.set_state(ENGINE_FAULT_KEY, "DashboardApiUnavailable")
            try:
                await asyncio.to_thread(bot.stop_engine)
            except Exception:
                LOGGER.exception("Could not verify bot order cleanup after API loss.")

    api_watchdog = (asyncio.create_task(stop_if_api_exits(), name="api-watchdog")
                    if ready else None)
    market_data_task = asyncio.create_task(
        _refresh_market_data(bot, app.state), name="market-intelligence"
    )
    try:
        if not ready:
            try:
                await bot.run(notifier)
            except RunnerLeaseLost:
                raise
            except Exception as error:
                await _ensure_fault_cleanup(bot, error)
                raise
        else:
            idle_failures = 0
            while True:
                bot.assert_runner_lease()
                if api_task.done():
                    try:
                        await asyncio.to_thread(bot.stop_engine)
                    except Exception:
                        LOGGER.exception("Could not verify bot order cleanup after API loss.")
                    raise TradingHalt("Dashboard API stopped in ready mode.")
                if bot.database.get_state(ENGINE_STATUS_KEY) == ENGINE_RUNNING:
                    try:
                        await bot.run(notifier)
                        if bot.stop_controller.stop_requested.is_set():
                            with bot.database.transaction():
                                bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE)
                                bot.database.set_state(ENGINE_FAULT_KEY, "ProcessStopping")
                    except RunnerLeaseLost:
                        raise
                    except Exception as error:
                        LOGGER.exception("Trading engine faulted; dashboard stays online.")
                        await _ensure_fault_cleanup(bot, error)
                        bot.database.set_state(ENGINE_FAULT_KEY, type(error).__name__)
                else:
                    delay = max(bot.config.poll_seconds, 5)
                    try:
                        await asyncio.to_thread(bot.reconcile_idle_orders)
                        idle_failures = 0
                    except (ccxt.BaseError, OSError, RuntimeError, sqlite3.Error) as error:
                        LOGGER.warning("Idle order cleanup will retry: %s",
                                       type(error).__name__)
                        delay = exchange_retry_delay(bot.exchange, error,
                            max(delay, network_backoff(idle_failures)))
                        idle_failures += 1
                    await asyncio.sleep(delay)
    finally:
        market_data_task.cancel()
        await asyncio.gather(market_data_task, return_exceptions=True)
        server.should_exit = True
        try:
            await asyncio.wait_for(
                asyncio.gather(api_task, return_exceptions=True), timeout=10
            )
        except asyncio.TimeoutError:
            api_task.cancel()
            await asyncio.gather(api_task, return_exceptions=True)
        finally:
            if api_watchdog is not None:
                api_watchdog.cancel()
                await asyncio.gather(api_watchdog, return_exceptions=True)
            app.state.grid_bot = None
            app.state.ready = False
            app.state.standby = False
            app.state.atr_snapshot = None
            app.state.order_book_snapshot = None


async def _run_standby_services(bot: GridBot) -> None:
    bot.acquire_runner_lease()
    bind_notification_loop()
    schedule_telegram_alert("*Process START / RESTART*\nStandby service initialized")
    heartbeat = asyncio.create_task(_runner_heartbeat(bot), name="runner-lease")
    try:
        await _run_standby_services_owned(bot)
        if bot._runner_lease_lost:
            raise RunnerLeaseLost("Trading runner lease was lost.")
    except Exception as error:
        if not getattr(error, "_telegram_alert_scheduled", False):
            await TelegramNotifier().notify_critical_error(error)
        raise
    finally:
        heartbeat.cancel()
        await asyncio.gather(heartbeat, return_exceptions=True)
        bot.release_runner_lease()
        await close_notifications()


async def _run_standby_services_owned(bot: GridBot) -> None:
    bot.database.set_state(ENGINE_STATUS_KEY, ENGINE_IDLE, trigger_event="STANDBY_IDLE")
    """Wait for an explicit grid request while serving the API and market data."""
    app.state.grid_bot = bot
    app.state.standby = True
    app.state.atr_snapshot = None
    app.state.order_book_snapshot = None
    market_data_task = asyncio.create_task(
        _refresh_market_data(bot, app.state), name="standby-market-intelligence"
    )
    server = uvicorn.Server(uvicorn.Config(
        app, host="127.0.0.1", port=8000, log_level="warning",
    ))
    api_task = asyncio.create_task(server.serve(), name="standby-api")
    try:
        while not bot.grid_activation_requested:
            bot.assert_runner_lease()
            if api_task.done():
                await api_task
                raise TradingHalt("Standby API stopped before grid activation.")
            await asyncio.sleep(0.25)
        app.state.standby = False
        await _reconcile_service_boot(bot, ready=False)
        notifier = TelegramNotifier()
        try:
            await bot.run(notifier)
        except RunnerLeaseLost:
            raise
        except Exception as error:
            await _ensure_fault_cleanup(bot, error)
            raise
    finally:
        server.should_exit = True
        await asyncio.gather(api_task, return_exceptions=True)
        market_data_task.cancel()
        await asyncio.gather(market_data_task, return_exceptions=True)
        app.state.grid_bot = None
        app.state.standby = False
        app.state.atr_snapshot = None
        app.state.order_book_snapshot = None


def main() -> int:
    handler = logging.StreamHandler()
    handler._grid_bot_handler = True
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOGGER.addHandler(handler)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    for module_name in ("engine", "api", "market_data", "portfolio", "stop_controller", "macro_trend"):
        module_logger = logging.getLogger(module_name)
        module_logger.setLevel(logging.INFO)
        for previous_handler in list(module_logger.handlers):
            if getattr(previous_handler, "_grid_bot_handler", False):
                module_logger.removeHandler(previous_handler)
        module_logger.addHandler(handler)
        module_logger.propagate = False
    parser = argparse.ArgumentParser(description="Binance Spot geometric grid bot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="Center and preview without orders")
    mode.add_argument("--execute", action="store_true", help="Trade on Spot")
    mode.add_argument("--monitor-only", action="store_true",
                      help="Serve the dashboard API without starting the trading bot")
    mode.add_argument("--standby", action="store_true",
                      help="Start the bot idle with market data, without trading")
    mode.add_argument("--ready", action="store_true",
                      help="Serve the dashboard; trading starts only from the web UI")
    arguments = parser.parse_args()
    try:
        if arguments.monitor_only:
            app.state.grid_bot = None
            app.state.monitor_only = True
            uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
            return 0
        if arguments.ready:
            _credentials()
            exchange = create_exchange()
            database = GridDatabase()
            existing_run = database.get_state("grid_run") is not None
            if not existing_run and (any(database.get_state(key) is not None for key in (
                    "grid_reset", "active_grid_config", SAFETY_MODE_KEY,
                    LIQUIDATION_KEY, TRAILING_STOP_KEY)) or database.fetch_all_orders()):
                raise TradingHalt("A saved bot state exists without a grid run; review it first.")
            config = (_apply_active_grid_config(GridConfig.load(preserve_stop=True), database)
                      if existing_run else GridConfig.load())
            bot = GridBot(config, exchange, database, macro_exchange=create_macro_exchange())
            bot.ready_mode = True
            asyncio.run(_run_services(bot, None, ready=True))
            return 0
        if arguments.standby:
            _credentials()
            exchange = create_exchange()
            database = GridDatabase()
            existing_run = database.get_state("grid_run") is not None
            if existing_run:
                config = _apply_active_grid_config(GridConfig.load(preserve_stop=True), database)
                bot = GridBot(config, exchange, database, macro_exchange=create_macro_exchange())
                bot.ready_mode = True
                asyncio.run(_run_services(bot, None, ready=True))
            else:
                if (any(database.get_state(key) is not None for key in (
                        "grid_reset", "active_grid_config", SAFETY_MODE_KEY,
                        LIQUIDATION_KEY, TRAILING_STOP_KEY)) or
                        database.fetch_all_orders()):
                    raise TradingHalt("Standby requires a fresh, empty bot database.")
                bot = GridBot(GridConfig.load(), exchange, database, macro_exchange=create_macro_exchange())
                if bot._live_open_orders():
                    raise TradingHalt("Standby requires no open BTC/USDT orders.")
                asyncio.run(_run_standby_services(bot))
            return 0
        _credentials()
        exchange = create_exchange()
        database = GridDatabase()
        runner_token = uuid.uuid4().hex
        runner_epoch = database.acquire_runner_lease(
            runner_token, ttl_seconds=RUNNER_LEASE_TTL_SECONDS
        )
        if runner_epoch is None:
            raise RunnerLeaseLost("Another process holds the trading runner lease.")
        try:
            _center_config_at_current_price(exchange, database)
            config = _apply_active_grid_config(GridConfig.load(
                preserve_stop=database.get_state('grid_run') is not None), database)
            bot = GridBot(config, exchange, database, runner_token=runner_token,
                          macro_exchange=create_macro_exchange())
            bot._runner_epoch = runner_epoch
            bot._lease_required = True
            if arguments.check:
                current, planned, upper_planned = bot.prepare(persist=False)
                print(f"Spot {TRADING_SETTINGS.environment.value} {config.symbol}: current={current}, anchor={bot.anchor}")
                print(f"Seed market buy: {bot._seed_quote()} {bot.market['quote']}")
                print(f"Geometric lower buy levels: {len(planned)}")
                for level, price, amount in planned:
                    print(f"  {level}: buy {amount} at {price}")
                print(f"Geometric upper sell levels: {len(upper_planned)}")
                for level, price, amount in upper_planned:
                    print(f"  {level}: sell {amount} at {price}")
                print(
                    "Total planned quote: "
                    f"{bot._seed_quote() + sum((p * a for _, p, a in planned), Decimal(0))}"
                )
                return 0
            if database.get_state(ENGINE_STATUS_KEY) in (ENGINE_IDLE, ENGINE_HALTED) or (
                    database.get_state(ENGINE_STOP_CLEANUP_KEY) == "1"):
                raise TradingHalt("Saved engine is IDLE, HALTED, or needs cleanup; use --ready.")
            asyncio.run(_run_services(bot, None))
            return 0
        finally:
            database.release_runner_lease(runner_token, runner_epoch)
    except ccxt.NetworkError as error:
        print(f"Bot stopped: {error}", file=sys.stderr)
        return 1
    except (ValueError, TradingHalt, ccxt.BaseError) as error:
        print(f"Bot stopped: {error}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. Check open orders before restarting.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
