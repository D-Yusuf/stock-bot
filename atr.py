from ib_async import Stock, BarData
from config import AppContext
from logger import log


async def get_atr(ctx: AppContext, contract: Stock, cfg: dict) -> float | None:
    period = cfg["atr"]["period"]
    try:
        bars: list[BarData] = await ctx.ib.reqHistoricalDataAsync(
            contract,
            endDateTime='',
            durationStr=f'{period + 5} D',
            # TO USE INTRADAY ATR: change barSizeSetting to '15 mins' and durationStr to '2 D'
            # Intraday ATR gives tighter SL/TP — better suited for same-day trades
            barSizeSetting='1 day',
            whatToShow='TRADES',
            useRTH=True,
            formatDate=1,
        )
        if len(bars) < period + 1:
            log(f"  ATR: not enough bars for {contract.symbol} ({len(bars)})", "warning")
            return None

        true_ranges = []
        for i in range(1, len(bars)):
            tr = max(
                bars[i].high - bars[i].low,
                abs(bars[i].high - bars[i - 1].close),
                abs(bars[i].low  - bars[i - 1].close),
            )
            true_ranges.append(tr)

        atr = sum(true_ranges[-period:]) / period
        log(f"  ATR({period}) {contract.symbol}: ${atr:.2f}")
        return atr
    except Exception as e:
        log(f"  ATR failed for {contract.symbol}: {e}", "error")
        return None
