FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --gid 1000 bot \
    && useradd --uid 1000 --gid bot --create-home bot \
    && mkdir /data \
    && chown bot:bot /data

COPY --chown=bot:bot main.py api.py engine.py models.py settings.py portfolio.py market_data.py telemetry.py notifier.py database.py exchange_handler.py trading_environment.py stop_controller.py telegram_bot.py docker_runner.py grid_geometry.py strategy_policy.py production_baseline.py allocation.py budget_policy.py dashboard_snapshot.py dashboard_reads.py macro_trend.py config.json ./
COPY --chown=bot:bot market_regime.py execution_history.py stop_history.py ./
COPY --chown=bot:bot inventory_sizing.py ./
COPY --chown=bot:bot campaign_risk.py ./
COPY --chown=bot:bot auto_atr.py ./
COPY --chown=bot:bot capital_recycling.py ./
COPY --chown=bot:bot fee_validation.py ./

USER bot
CMD ["python", "docker_runner.py"]
