from config import AppContext
from logger import log


async def get_total_capital(ctx: AppContext) -> float:
    for v in ctx.ib.accountValues():
        if v.tag == 'NetLiquidation' and v.currency == 'USD' and v.account == ctx.ib_acc:
            return float(v.value)
    return 0.0


async def get_available_cash(ctx: AppContext) -> float:
    """Returns settled cash available to trade. Uses SettledCash on cash accounts
    to avoid counting unsettled T+1 sale proceeds that IBKR won't let you spend."""
    settled = None
    total   = None
    for v in ctx.ib.accountValues():
        if v.currency != 'USD' or v.account != ctx.ib_acc:
            continue
        if v.tag == 'SettledCash':
            settled = float(v.value)
        elif v.tag == 'TotalCashValue':
            total = float(v.value)
    result = settled if settled is not None else (total or 0.0)
    if settled is not None and total is not None and settled != total:
        log(f"  Cash: total=${total:.2f}, settled=${settled:.2f} — using settled (T+1 constraint)")
    return result
