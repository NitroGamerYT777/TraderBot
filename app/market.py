"""Sampled market analytics. Never mix simulated and broker data."""
import math
import random
import time
from collections import deque

COINS = [
    ("BTC", "Bitcoin", 67482), ("ETH", "Ethereum", 2641),
    ("SOL", "Solana", 172.4), ("AVAX", "Avalanche", 28.72),
    ("DOGE", "Dogecoin", .138), ("LINK", "Chainlink", 12.4),
    ("LTC", "Litecoin", 71.3), ("BCH", "Bitcoin Cash", 364),
    ("UNI", "Uniswap", 7.82), ("AAVE", "Aave", 149.2),
    ("DOT", "Polkadot", 4.21), ("SHIB", "Shiba Inu", .000018),
    ("XRP", "XRP", .529), ("ADA", "Cardano", .356),
    ("ATOM", "Cosmos", 4.67), ("BAT", "Basic Attention Token", .172),
    ("CRV", "Curve", .263), ("GRT", "The Graph", .158),
    ("MKR", "Maker", 1208), ("SUSHI", "SushiSwap", .71),
    ("XTZ", "Tezos", .674), ("YFI", "yearn.finance", 4860),
    ("USDC", "USD Coin", 1), ("USDT", "Tether", 1),
    ("PEPE", "Pepe", .0000098), ("TRUMP", "Official Trump", 8.2),
]


def correlation(a, b):
    if len(a) < 30 or len(a) != len(b):
        return None
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    va = sum((x - ma) ** 2 for x in a)
    vb = sum((y - mb) ** 2 for y in b)
    if va <= 1e-24 or vb <= 1e-24:
        return None
    return max(-1, min(1, sum((x-ma)*(y-mb) for x, y in zip(a,b)) / math.sqrt(va*vb)))


class Market:
    def __init__(self, demo=True):
        self.demo = demo
        self.assets = []
        self.quotes = {}
        self.series = deque(maxlen=601)
        self.status = "demo" if demo else "connecting"
        self.message = "Simulyatsiya · haqiqiy narxlar emas" if demo else "Alpaca oqimi kutilmoqda"
        self.stream_error = None
        self.rest_error = None
        self.subscribed_symbols = []
        self.last_ws_quote_received = 0.
        self.last_rest_fetch = 0.
        self.clock = None
        self.rng = random.Random(42)
        if demo:
            now = time.time()
            self.assets = [{"symbol": f"{s}/USD", "name": n, "tradable": False} for s,n,_ in COINS]
            self.quotes = {f"{s}/USD": {"price": p, "timestamp": now, "baseline": p} for s,_,p in COINS}
            for i in range(360):
                self.demo_tick(now - 360 + i)
                self.sample(now - 360 + i)

    def demo_tick(self, timestamp=None):
        common = self.rng.gauss(0, .00026)
        for s, q in self.quotes.items():
            movement = self.rng.gauss(0, .000005) if s.startswith(("USDC/", "USDT/")) else common + self.rng.gauss(0, .00023)
            q["price"] *= 1 + movement
            q["timestamp"] = timestamp or time.time()

    def update_quote(self, message, source="websocket"):
        s = message.get("S")
        try:
            bid, ask = float(message.get("bp", 0)), float(message.get("ap", 0))
        except (TypeError, ValueError):
            return False
        if s and math.isfinite(bid) and math.isfinite(ask) and bid > 0 and ask >= bid:
            from datetime import datetime
            try:
                stamp = datetime.fromisoformat(message["t"].replace("Z", "+00:00")).timestamp()
            except (KeyError, ValueError, TypeError, AttributeError, OverflowError):
                return False
            if stamp>self.now()+30:
                return False
            previous = self.quotes.get(s,{})
            if stamp < previous.get("timestamp", 0):
                return False
            if source=="rest" and stamp==previous.get("timestamp") and previous.get("source")=="websocket":
                return False
            price = (bid + ask) / 2
            self.quotes[s] = {"price": price, "bid":bid,"ask":ask,"timestamp": stamp, "baseline": previous.get("baseline", price),"source":source}
            if source=="websocket":
                self.last_ws_quote_received = time.monotonic()
            return True
        return False

    def now(self):
        return self.clock() if self.clock else time.time()

    def quote_age(self, quote):
        return self.now()-quote.get("timestamp",0)

    def health(self):
        if self.demo:
            return {"status":self.status,"message":self.message,"stream_error":None,"rest_error":None,"data_source":"demo","subscribed_symbols":[]}
        now = time.monotonic()
        fresh = any(-30<=self.quote_age(q)<=15 for q in self.quotes.values())
        fresh_stream = any(q.get("source")=="websocket" and -30<=self.quote_age(q)<=15 for q in self.quotes.values())
        if self.status=="connected" and now-self.last_ws_quote_received<=20 and fresh_stream:
            status,message,source = "connected","Alpaca · WebSocket jonli kotirovkalar","websocket"
        elif now-self.last_rest_fetch<=20 and fresh:
            status,message,source = "rest","Alpaca · REST narxlari (10 soniyada yangilanadi)","rest"
        else:
            status,message,source = ("waiting","Ulanish bor, yangi narxlar kutilmoqda…","none") if self.status=="connected" else (self.status,self.message,"none")
        skew = round(time.time()-self.now())
        warning = f"Kompyuter va Alpaca soati orasida ≈ {abs(skew)/3600:.1f} soat farq bor. Narxlar Alpaca serveri vaqti bilan tekshirilmoqda; kompyuter soatini tekshiring." if abs(skew)>60 else None
        return {"status":status,"message":message,"stream_error":self.stream_error,"rest_error":self.rest_error,
                "data_source":source,"subscribed_symbols":list(self.subscribed_symbols),"clock_warning":warning,"clock_skew_seconds":skew}

    def sample(self, now=None):
        now = now or self.now()
        if self.series and now<self.series[-1]["time"]:
            self.series.clear()
        self.series.append({"time": now, "prices": {s:q["price"] for s,q in self.quotes.items() if now-q["timestamp"] <= 15}})

    def analytics(self, symbols):
        # Consecutive synchronized one-second intervals, excluding stale/missing quotes.
        rows = list(self.series)
        returns = {s: [] for s in symbols}
        for prev, curr in zip(rows, rows[1:]):
            if curr["time"] - prev["time"] > 2.5:
                continue
            if all(s in prev["prices"] and s in curr["prices"] for s in symbols):
                for s in symbols:
                    returns[s].append(math.log(curr["prices"][s] / prev["prices"][s]))
        matrix = [[correlation(returns[a], returns[b]) for b in symbols] for a in symbols]
        chart = []
        bases = {}
        for row in rows:
            item = {"time": row["time"]}
            for s in symbols:
                p = row["prices"].get(s)
                if p:
                    bases.setdefault(s, p)
                    item[s] = (p / bases[s] - 1) * 100
            chart.append(item)
        return {"symbols": symbols, "matrix": matrix, "samples": len(next(iter(returns.values()), [])), "chart": chart}

    def snapshot(self):
        now = self.now()
        return [{**a, **self.quotes.get(a["symbol"], {}), "stale": now-self.quotes.get(a["symbol"], {}).get("timestamp", 0) > 15,
                 "change": ((self.quotes[a["symbol"]]["price"] / self.quotes[a["symbol"]]["baseline"] - 1)*100) if a["symbol"] in self.quotes else None}
                for a in self.assets]
