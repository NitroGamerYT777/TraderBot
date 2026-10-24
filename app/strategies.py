"""Deterministic, long-only rules shared by the engine and historical simulation."""
import math
from datetime import datetime

MINUTES = {"5Min":5,"15Min":15,"1Hour":60}
FEE_RATE = .0025  # Estimated taker fee per side; actual fee tier can differ.
SLIPPAGE = .001


def stamp(value):
    return datetime.fromisoformat(value.replace("Z","+00:00")).timestamp()


def closed_bars(bars, timeframe, now):
    result = {}
    seconds = MINUTES[timeframe]*60
    for bar in bars:
        try:
            t = stamp(bar["t"])
            if t+seconds+2>now:
                continue
            o,h,l,c = (float(bar[k]) for k in ("o","h","l","c"))
            if not all(math.isfinite(x) and x>0 for x in (o,h,l,c)) or not l<=min(o,c)<=max(o,c)<=h:
                continue
            result[t] = {"time":t,"o":o,"h":h,"l":l,"c":c}
        except (KeyError, ValueError, TypeError):
            continue
    return [result[t] for t in sorted(result)]


def bars_fresh(bars,timeframe,now):
    # A closed 15-minute bar remains the newest valid bar throughout the next
    # 15-minute interval, plus a short publication grace period.
    return bool(bars) and bars[-1]["time"]+2*MINUTES[timeframe]*60+90>=now


def ema(values, period):
    result, value = [], values[0]
    for price in values:
        value += (price-value)*2/(period+1)
        result.append(value)
    return result


def rsi(values, period=14):
    if len(values)<=period:
        return None
    diffs = [b-a for a,b in zip(values,values[1:])]
    gain = sum(max(d,0) for d in diffs[:period])/period
    loss = sum(max(-d,0) for d in diffs[:period])/period
    for d in diffs[period:]:
        gain = (gain*(period-1)+max(d,0))/period
        loss = (loss*(period-1)+max(-d,0))/period
    return 50 if gain==loss==0 else 100 if loss==0 else 100-100/(1+gain/loss)


def signal(bars):
    if len(bars)<100:
        return {"action":"wait","reason":"EMA uchun kamida 100 ta yopilgan sham kerak."}
    values = [b["c"] for b in bars]
    fast, slow, strength = ema(values,20), ema(values,50), rsi(values)
    buy = fast[-2]<=slow[-2] and fast[-1]>slow[-1] and 45<=strength<=70
    sell = fast[-1]<slow[-1]
    return {"action":"buy" if buy else "sell" if sell else "wait", "ema20":fast[-1],"ema50":slow[-1],"rsi":strength,
            "reason":"EMA20 yuqoriga kesishdi, RSI mos." if buy else "EMA20 EMA50 dan pastda." if sell else "Yangi EMA kesishish signali kutilmoqda."}


def position_size(config, cash):
    # Reserve estimated fees/slippage in the risk budget; this is not a loss guarantee.
    risk_fraction = config["stop_loss_pct"]/100 + 2*(FEE_RATE+SLIPPAGE)
    return max(0,min(config["max_position_usd"],cash/(1+FEE_RATE+SLIPPAGE),config["budget_usd"]*config["risk_pct"]/100/risk_fraction))


def backtest(bars, config):
    """No look-ahead: signals at close, fills at next open. Stop wins ambiguous OHLC bars."""
    cash, qty, cost, entry = config["budget_usd"],0.,0.,0.
    trades, curve, last_exit = [],[], -1000
    peak, drawdown, wins = cash,0.,0
    day, day_start, daily_entries, day_locked = None,cash,0,False
    for i in range(100,len(bars)):
        bar = bars[i]
        key = int(bar["time"]//86400)
        if key!=day:
            day,day_start,daily_entries,day_locked = key,cash+qty*bar["o"]*(1-FEE_RATE),0,False
        day_locked |= cash+qty*bar["o"]*(1-FEE_RATE)<=day_start-config["budget_usd"]*config["daily_loss_pct"]/100
        decision = signal(bars[max(0,i-349):i])
        exit_price, reason = None,None
        if qty:
            stop, take = entry*(1-config["stop_loss_pct"]/100),entry*(1+config["take_profit_pct"]/100)
            if day_locked or decision["action"]=="sell":
                exit_price,reason = bar["o"],"Kunlik limit" if day_locked else "Trend qaytdi"
            elif bar["l"]<=stop:
                exit_price,reason = min(bar["o"],stop),"Stop-loss"
            elif bar["h"]>=take:
                exit_price,reason = take,"Take-profit"
            if exit_price is not None:
                net = qty*exit_price*(1-SLIPPAGE)*(1-FEE_RATE)
                pnl = net-cost
                cash += net
                wins += pnl>0
                trades.append({"time":bar["time"],"pnl":round(pnl,4),"reason":reason})
                qty,cost,last_exit = 0.,0.,i
        elif decision["action"]=="buy" and not day_locked and daily_entries<config["max_daily_trades"] and i-last_exit>=config["cooldown_bars"]:
            notional = position_size(config,cash)
            entry = bar["o"]*(1+SLIPPAGE)
            cost = notional*(1+FEE_RATE)
            cash -= cost
            qty = notional/entry
            daily_entries += 1
            # Entry is at this bar's open: its high/low follow the fill. If both
            # limits are touched, assume the adverse stop was reached first.
            stop,take = entry*(1-config["stop_loss_pct"]/100),entry*(1+config["take_profit_pct"]/100)
            if bar["l"]<=stop or bar["h"]>=take:
                exit_price = min(bar["o"],stop) if bar["l"]<=stop else take
                net = qty*exit_price*(1-SLIPPAGE)*(1-FEE_RATE)
                pnl = net-cost
                cash += net
                wins += pnl>0
                trades.append({"time":bar["time"],"pnl":round(pnl,4),"reason":"Stop-loss" if bar["l"]<=stop else "Take-profit"})
                qty,cost,last_exit = 0.,0.,i
        equity = cash+qty*bar["c"]*(1-FEE_RATE)
        peak = max(peak,equity)
        drawdown = max(drawdown,(peak-equity)/peak*100)
        curve.append({"time":bar["time"],"equity":round(equity,4)})
    equity = cash+qty*bars[-1]["c"]*(1-FEE_RATE) if bars else cash
    return {"bars":len(bars),"trades":len(trades),"win_rate":round(wins/len(trades)*100,2) if trades else None,
            "return_pct":round((equity/config["budget_usd"]-1)*100,3),"max_drawdown_pct":round(drawdown,3),
            "equity":round(equity,2),"open_position":bool(qty),"curve":curve,"recent_trades":trades[-10:],
            "fee_pct":FEE_RATE*100,"slippage_pct":SLIPPAGE*100,
            "start":bars[0]["time"] if bars else None,"end":bars[-1]["time"] if bars else None,
            "note":"Cheklangan tarixiy simulyatsiya. Har tomonga 0.25% taxminiy komissiya va 0.10% slippage. Live natija kafolati emas."}
