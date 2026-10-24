import asyncio
import json
import math
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime
import httpx
import websockets

BASES = {"paper": "https://paper-api.alpaca.markets", "live": "https://api.alpaca.markets"}
MAX_STREAM_SYMBOLS = 20
STREAM_ERRORS = {
    400:"WebSocket obuna formati yoki juftligi rad etildi",
    401:"WebSocket autentifikatsiya talab qilyapti",
    402:"WebSocket kalitlarni qabul qilmadi",
    403:"WebSocket allaqachon autentifikatsiyadan o‘tgan",
    404:"WebSocket autentifikatsiya vaqti tugadi",
    405:"WebSocket aktivlar limiti oshdi; obuna qisqartirilmoqda",
    406:"WebSocket ulanish limiti band; boshqa ulanishni yoping yoki REST narxlaridan foydalaning",
    407:"WebSocket mijoz oqimni qabul qilishga ulgurmayapti",
    409:"WebSocket ma’lumot oqimiga obuna ruxsati yo‘q",
    410:"WebSocket kanali bu oqimda mavjud emas",
}


class BrokerError(Exception):
    def __init__(self, message, status=None, uncertain=False):
        super().__init__(message)
        self.status, self.uncertain = status, uncertain


class Alpaca:
    def __init__(self, key, secret, mode):
        self.key, self.secret, self.mode = key, secret, mode
        self.client = httpx.AsyncClient(timeout=15, headers={"APCA-API-KEY-ID":key, "APCA-API-SECRET-KEY":secret})
        self.quote_unavailable = {}
        self.data_clock_anchor = None

    def market_time(self):
        if self.data_clock_anchor:
            monotonic, utc = self.data_clock_anchor
            return utc+time.monotonic()-monotonic
        return time.time()

    async def request(self, method, path, params=None, payload=None, data=False):
        try:
            base = "https://data.alpaca.markets" if data else BASES[self.mode]
            started = time.monotonic()
            response = await self.client.request(method, base+path, params=params, json=payload)
            response.raise_for_status()
            if data and time.monotonic()-started<5:
                # TLS-authenticated provider time; never change the Windows clock.
                # Anchor it to monotonic time so local clock jumps cannot age quotes.
                try:
                    server = parsedate_to_datetime(response.headers["date"]).timestamp()
                    age = max(0,float(response.headers.get("age",0)))
                    calibrated = server+age+(time.monotonic()-started)/2
                    if math.isfinite(calibrated):
                        # HTTP Date has one-second precision: repeated observations
                        # must not move the quote clock backwards and reset charts.
                        if self.data_clock_anchor:
                            calibrated = max(calibrated,self.market_time())
                        self.data_clock_anchor = (time.monotonic(),calibrated)
                except (KeyError,TypeError,ValueError,OverflowError):
                    pass
            return response.json() if response.content else None
        except httpx.HTTPStatusError as exc:
            messages = {401:"API Key yoki Secret Key noto‘g‘ri. Paper/Live rejimini tekshiring.",403:"Akkauntda ushbu ma’lumot uchun ruxsat yo‘q.",429:"Alpaca so‘rov limiti. Birozdan keyin qayta urinib ko‘ring."}
            status = exc.response.status_code
            raise BrokerError(messages.get(status, "Alpaca so‘rovni qabul qilmadi. Order va akkaunt holatini tekshiring."), status=status, uncertain=status>=500 or status==408) from None
        except (httpx.RequestError, ValueError):
            message = "Alpaca bilan aloqa yo‘q. Order holati tekshirilmaguncha qayta yuborilmaydi." if method=="POST" else "Alpaca bilan aloqa yo‘q. Internetni tekshiring."
            raise BrokerError(message, uncertain=True) from None

    async def get(self, path, params=None):
        return await self.request("GET", path, params=params)

    async def latest_quotes(self, symbols):
        result = await self.request("GET","/v1beta3/crypto/us/latest/quotes",params={"symbols":",".join(symbols)},data=True)
        return result.get("quotes",{})

    async def quote_batch(self, symbols):
        try:
            return await self.latest_quotes(symbols)
        except BrokerError as exc:
            if exc.status not in (400,422):
                raise
            if len(symbols)==1:
                self.quote_unavailable[symbols[0]] = time.time()+300
                return {}
            middle = len(symbols)//2
            return {**await self.quote_batch(symbols[:middle]),**await self.quote_batch(symbols[middle:])}

    async def refresh_quotes_once(self, market):
        # REST fills the full watch table and keeps prices available if streaming fails.
        symbols = [a["symbol"] for a in market.assets if self.quote_unavailable.get(a["symbol"],0)<=time.time()]
        for offset in range(0,len(symbols),25):
            quotes = await self.quote_batch(symbols[offset:offset+25])
            for symbol,quote in quotes.items():
                market.update_quote({**quote,"S":symbol},source="rest")
        market.last_rest_fetch = time.monotonic()
        market.rest_error = None

    async def poll_quotes(self,market):
        delay = 10
        while True:
            try:
                await self.refresh_quotes_once(market)
                delay = 10
            except asyncio.CancelledError:
                raise
            except BrokerError as exc:
                market.rest_error = str(exc)
                delay = min(delay*2,60)
            except Exception:
                market.rest_error = "Alpaca REST narxlarini o‘qib bo‘lmadi. Qayta urinilmoqda."
                delay = min(delay*2,60)
            await asyncio.sleep(delay)

    async def account(self):
        a = await self.get("/v2/account")
        return {"id":a.get("id"), "equity":float(a.get("equity") or 0), "cash":float(a.get("cash") or 0),
                "buying_power":float(a.get("buying_power") or 0), "last_equity":float(a.get("last_equity") or 0),
                "status":a.get("status"), "currency":a.get("currency", "USD"),
                "trading_blocked":a.get("trading_blocked", False),"account_blocked":a.get("account_blocked",False)}

    async def assets(self):
        assets = await self.get("/v2/assets", {"asset_class":"crypto", "status":"active"})
        return sorted([{"symbol":a["symbol"], "name":a.get("name", a["symbol"]), "tradable":a.get("tradable",False),
                        "min_order_size":a.get("min_order_size"),"min_trade_increment":a.get("min_trade_increment")} for a in assets if a.get("tradable")],key=lambda a:(a["symbol"] not in ("BTC/USD","ETH/USD","SOL/USD"),a["symbol"]))

    async def bars(self, symbol, timeframe, limit=350):
        minutes = {"5Min":5,"15Min":15,"1Hour":60}[timeframe]
        start = datetime.fromtimestamp(self.market_time(),timezone.utc)-timedelta(minutes=minutes*(limit+10))
        params = {"symbols":symbol,"timeframe":timeframe,"start":start.isoformat(),"limit":limit,"sort":"desc"}
        bars, seen = [],set()
        for _ in range(12):
            result = await self.request("GET","/v1beta3/crypto/us/bars",params=params,data=True)
            bars.extend(result.get("bars",{}).get(symbol,[]))
            token = result.get("next_page_token")
            if len(bars)>=limit or not token:
                return sorted(bars[:limit],key=lambda b:b["t"])
            if token in seen:
                break
            seen.add(token)
            params.update(page_token=token,limit=limit-len(bars))
        raise BrokerError("Tarixiy shamlar sahifalarini to‘liq olib bo‘lmadi. Keyinroq qayta urinib ko‘ring.")

    async def positions(self):
        return await self.get("/v2/positions")

    async def open_orders(self):
        return await self.get("/v2/orders",{"status":"open","limit":500,"nested":"true"})

    async def submit_order(self, symbol, side, qty, client_order_id):
        # Never retry POST automatically. A timeout is reconciled using client_order_id.
        return await self.request("POST","/v2/orders",payload={"symbol":symbol,"side":side,"qty":qty,
                    "type":"market","time_in_force":"gtc","client_order_id":client_order_id})

    async def order_by_client_id(self, client_order_id):
        return await self.get("/v2/orders:by_client_order_id",{"client_order_id":client_order_id})

    async def cancel_order(self, order_id):
        from urllib.parse import quote
        return await self.request("DELETE","/v2/orders/"+quote(order_id,safe=""))

    async def history(self, period):
        params = {"period":period,"timeframe":"5Min" if period=="1D" else "1H" if period=="1W" else "1D", "intraday_reporting":"continuous", "pnl_reset":"no_reset"}
        data = await self.get("/v2/account/portfolio/history", params)
        return [{"time":t,"equity":v} for t,v in zip(data.get("timestamp",[]),data.get("equity",[])) if v is not None]

    async def stream(self, market, get_symbols=None):
        delay = 1
        capacity = MAX_STREAM_SYMBOLS
        while True:
            try:
                market.status, market.message = "connecting", "Alpaca WebSocket ulanmoqda…"
                async with websockets.connect("wss://stream.data.alpaca.markets/v1beta3/crypto/us", open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"action":"auth","key":self.key,"secret":self.secret}))
                    authenticated = False
                    requested = set()
                    started = last_received = time.monotonic()
                    while True:
                        if authenticated:
                            available = {a["symbol"] for a in market.assets}
                            focus = get_symbols() if get_symbols else [a["symbol"] for a in market.assets]
                            desired = list(dict.fromkeys(s for s in focus if s in available))[:capacity]
                            removed,added = requested-set(desired),set(desired)-requested
                            if removed:
                                await ws.send(json.dumps({"action":"unsubscribe","quotes":sorted(removed)}))
                            if added:
                                await ws.send(json.dumps({"action":"subscribe","quotes":[s for s in desired if s in added]}))
                            requested = set(desired)
                        try:
                            raw = await asyncio.wait_for(ws.recv(),timeout=1)
                            last_received = time.monotonic()
                        except asyncio.TimeoutError:
                            if not authenticated and time.monotonic()-started>10:
                                raise BrokerError("WebSocket autentifikatsiya javobi kelmadi",status=404)
                            if authenticated and time.monotonic()-last_received>30:
                                raise BrokerError("WebSocket narx oqimi jim qoldi; qayta ulanmoqda")
                            continue
                        for msg in json.loads(raw):
                            if msg.get("T") == "error":
                                code = msg.get("code")
                                if code==405:
                                    capacity = max(1,min(capacity-1,len(requested)//2 or 1))
                                raise BrokerError(f"{STREAM_ERRORS.get(code,'Alpaca WebSocket oqimi rad etildi')} ({code if isinstance(code,int) else 'noma’lum'})",status=code)
                            if msg.get("msg") == "authenticated":
                                authenticated = True
                            if authenticated and msg.get("T") == "subscription":
                                market.subscribed_symbols = msg.get("quotes",[])
                                market.stream_error = None
                                market.status, market.message = "connected", "Alpaca · jonli kotirovkalar"
                                delay = 1
                            if authenticated and msg.get("T") == "q":
                                market.update_quote(msg)
            except asyncio.CancelledError:
                raise
            except BrokerError as exc:
                market.status, market.message = "reconnecting", str(exc)
                market.stream_error = {"code":exc.status,"message":str(exc)}
            except Exception:
                # Never include remote messages or request objects: they can contain credentials.
                market.status, market.message = "reconnecting", "Narx oqimi uzildi. Avtomatik qayta ulanmoqda…"
                market.stream_error = {"code":None,"message":market.message}
            market.subscribed_symbols = []
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def close(self):
        await self.client.aclose()
