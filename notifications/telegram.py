"""Q-01 to Q-07: Telegram alert system."""
import asyncio
import time
from collections import defaultdict
import structlog
import config

log = structlog.get_logger()

_last_sent: dict[str, float] = defaultdict(float)
_RATE_LIMIT_SECONDS = 300


async def _send(message: str) -> None:
    from telegram import Bot
    bot = Bot(token=config.TELEGRAM_BOT_TOKEN)
    await bot.send_message(chat_id=config.TELEGRAM_CHAT_ID, text=message, parse_mode="HTML")


def send_critical(message: str) -> None:
    """Q-02: Send immediately, no rate limiting — for emergencies."""
    try:
        asyncio.get_event_loop().run_until_complete(_send(f"🚨 <b>CRITICAL</b>\n{message}"))
        log.info("telegram_critical_sent")
    except Exception as exc:
        log.error("telegram_send_failed", error=str(exc))


def send_trade_alert(event_type: str, data: dict) -> None:
    """Q-03: Send per-trade event with formatted message."""
    now = time.time()
    key = f"trade_{event_type}"
    if now - _last_sent[key] < _RATE_LIMIT_SECONDS:
        return
    _last_sent[key] = now

    templates = {
        "opened": (
            "📈 <b>{direction} {pair}</b> opened\n"
            "@ ${entry_price:,.4f} | {leverage}x lev | {capital_pct}% capital\n"
            "Potential: {potential}/100"
        ),
        "dca": "🔄 DCA round {round} triggered on {pair} @ ${dca_price:,.4f}",
        "sl_moved": "📌 SL moved on {pair}: new SL @ ${new_sl:,.4f}",
        "closed": (
            "✅ <b>{pair}</b> closed\n"
            "Net PnL: ${net_pnl:+.2f} | Hold: {hold_hours:.1f}h | Fees: ${fees:.2f}"
        ),
    }
    template = templates.get(event_type, "{event_type}: {data}")
    try:
        msg = template.format(event_type=event_type, **data)
    except KeyError:
        msg = f"{event_type}: {data}"

    try:
        asyncio.get_event_loop().run_until_complete(_send(msg))
    except Exception as exc:
        log.error("telegram_trade_alert_failed", error=str(exc))


def send_daily_summary(data: dict) -> None:
    """Q-04: Daily summary message."""
    msg = (
        "📊 <b>Daily Summary</b>\n"
        f"Trades: {data.get('trades_opened',0)} opened / {data.get('trades_closed',0)} closed\n"
        f"Net PnL: ${data.get('daily_pnl', 0):+.2f}\n"
        f"Win Rate: {data.get('win_rate', 0):.1f}%\n"
        f"Brain Stage: {data.get('brain_stage', 1)}\n"
        f"Top Pair: {data.get('top_pair', 'N/A')}"
    )
    try:
        asyncio.get_event_loop().run_until_complete(_send(msg))
    except Exception as exc:
        log.error("telegram_daily_summary_failed", error=str(exc))


def send_weekly_report(data: dict) -> None:
    """Q-05: Weekly rolling performance report."""
    msg = (
        "📈 <b>Weekly Report</b>\n"
        f"Sharpe: {data.get('sharpe', 0):.2f}\n"
        f"Brain Stage: {data.get('brain_stage', 1)}"
    )
    try:
        asyncio.get_event_loop().run_until_complete(_send(msg))
    except Exception as exc:
        log.error("telegram_weekly_report_failed", error=str(exc))


def send_monthly_report(data: dict) -> None:
    """Q-06: Monthly performance summary."""
    msg = (
        "📅 <b>Monthly Report</b>\n"
        f"Net PnL: ${data.get('net_pnl', 0):+.2f}\n"
        f"Total Fees: ${data.get('total_fees', 0):.2f}\n"
        f"Best Strategy: {data.get('best_strategy', 'N/A')}"
    )
    try:
        asyncio.get_event_loop().run_until_complete(_send(msg))
    except Exception as exc:
        log.error("telegram_monthly_report_failed", error=str(exc))
