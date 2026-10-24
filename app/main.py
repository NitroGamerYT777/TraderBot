import asyncio
import contextlib
import json
import os
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

import keyring
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, SecretStr
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .alpaca import Alpaca, BrokerError
from .market import Market
from .bots import BotAction, BotConfig, BotEngine, BotError

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("AUTO_TRADER_DATA_DIR", ROOT / ".local"))
SERVICE = "AutoTrader.Alpaca"
TOKEN = secrets.token_urlsafe(32)


def load_settings():
    defaults = {"refresh_hz":2,"symbols":["BTC/USD","ETH/USD","SOL/USD","AVAX/USD"],"remember":False,"mode":"paper"}
    try:
        saved = json.loads((DATA / "settings.json").read_text())
        return {**defaults, **Settings(**saved).model_dump(), "remember":bool(saved.get("remember",False)), "mode": saved.get("mode") if saved.get("mode") in ("paper","live") else "paper"}
    except (OSError, ValueError, TypeError):
        return defaults


class Settings(BaseModel):
    refresh_hz: int = Field(default=2, ge=1, le=10)
    symbols: list[str] = Field(default=["BTC/USD","ETH/USD","SOL/USD","AVAX/USD"], min_length=1, max_length=6)


class Connection(BaseModel):
    api_key: SecretStr
    secret_key: SecretStr
    mode: Literal["paper","live"] = "paper"
    remember: bool = False


class Runtime:
    def __init__(self):
        self.settings = load_settings()
        self.market = Market()
        self.broker = None
        self.account = None
        self.account_updated = None
        self.account_error = None
        self.startup_error = None
        self.mode = "demo"
        self.jobs = []
        self.lock = asyncio.Lock()
        self.analytics_cache = None
        self.analytics_time = 0
        self.bots = BotEngine(self, DATA / "bots.sqlite3")

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = DATA / "settings.tmp"
        tmp.write_text(json.dumps(self.settings), encoding="utf-8")
        tmp.replace(DATA / "settings.json")

    async def stop_broker(self):
        for job in self.jobs:
            job.cancel()
        for job in self.jobs:
            with contextlib.suppress(asyncio.CancelledError):
                await job
        self.jobs = []
        if self.broker:
            await self.broker.close()
        self.broker = None

    async def refresh_account(self):
        while True:
            await asyncio.sleep(15)
            try:
                self.account = await self.broker.account()
                self.account_updated = time.time()
                self.account_error = None
            except BrokerError as exc:
                self.account_error = str(exc)

    async def connect(self, payload):
        broker = Alpaca(payload.api_key.get_secret_value().strip(),payload.secret_key.get_secret_value().strip(),payload.mode)
        try:
            account, assets = await asyncio.gather(broker.account(),broker.assets())
            self.bots.check_switch(account,payload.mode)
            if not assets:
                raise BrokerError("Akkaunt uchun savdoga ochiq kripto aktivlar topilmadi.")
            if payload.remember:
                try:
                    keyring.set_password(SERVICE, "credentials", json.dumps({"api_key":broker.key,"secret_key":broker.secret,"mode":payload.mode}))
                except Exception:
                    raise BrokerError("Tizimning xavfsiz kalit omboriga yozib bo‘lmadi. Saqlash belgisini o‘chirib qayta ulang.") from None
            else:
                forget_credentials(required=self.settings["remember"])
        except BaseException:
            await broker.close()
            raise
        await self.stop_broker()
        self.broker, self.account, self.mode = broker, account, payload.mode
        self.account_updated, self.account_error, self.startup_error = time.time(), None, None
        self.market = Market(demo=False)
        self.market.clock = broker.market_time
        self.market.assets = assets
        self.settings.update(remember=payload.remember, mode=payload.mode)
        valid = {a["symbol"] for a in assets}
        chosen = [s for s in self.settings["symbols"] if s in valid]
        self.settings["symbols"] = chosen or [a["symbol"] for a in assets[:4]]
        self.save()
        self.analytics_time = 0
        self.jobs = [asyncio.create_task(broker.stream(self.market,self.stream_symbols)),
                     asyncio.create_task(broker.poll_quotes(self.market)),asyncio.create_task(self.refresh_account())]

    def stream_symbols(self):
        active = [b["config"]["symbol"] for b in self.bots.items.values()
                  if b["config"]["mode"]==self.mode and (b["status"]=="running" or b["qty"]>0 or b["pending"])]
        return list(dict.fromkeys(active+self.settings["symbols"]+["BTC/USD","ETH/USD"]))

    def snapshot(self):
        if time.monotonic()-self.analytics_time >= 1 or self.analytics_cache is None:
            self.analytics_cache = self.market.analytics(self.settings["symbols"])
            self.analytics_time = time.monotonic()
        return {"mode":self.mode,**self.market.health(),
                "assets":self.market.snapshot(),"analytics":self.analytics_cache,"account":self.account,
                "account_updated":self.account_updated,"account_error":self.account_error,
                "startup_error":self.startup_error,"settings":self.settings,"server_time":time.time(),
                "bots":self.bots.public(),"bots_error":self.bots.storage_error}


def forget_credentials(required=False):
    try:
        keyring.delete_password(SERVICE, "credentials")
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.NoKeyringError:
        if required:
            raise BrokerError("Saqlangan kalitlar ombori mavjud emas. Tizim kalit omborini tiklang.") from None
    except Exception:
        raise BrokerError("Tizimdagi saqlangan kalitlarni o‘chirib bo‘lmadi. Tizim kalit omborini tekshiring.") from None


runtime = Runtime()


async def ticker():
    last_sample = 0
    while True:
        now = time.monotonic()
        if runtime.market.demo:
            runtime.market.demo_tick()
        if now-last_sample >= 1:
            runtime.market.sample()
            last_sample = now
        await asyncio.sleep(.1)


@asynccontextmanager
async def lifespan(app):
    runtime.bots.acquire()
    if runtime.settings["remember"]:
        try:
            raw = keyring.get_password(SERVICE,"credentials")
            if raw:
                async with runtime.lock:
                    await runtime.connect(Connection(**json.loads(raw),remember=True))
        except Exception:
            runtime.startup_error = "Saqlangan Alpaca ulanishi tiklanmadi. Account bo‘limida qayta ulang. Demo faol."
    task = asyncio.create_task(ticker())
    bot_task = asyncio.create_task(runtime.bots.run())
    yield
    bot_task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await bot_task
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await runtime.stop_broker()
    runtime.bots.release()


app = FastAPI(title="Auto Trader", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1","localhost","testserver"])


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default response includes rejected input; never echo API secrets.
    if request.url.path.startswith("/api/bots"):
        # Only application-defined validation text, never rejected request input.
        messages = [str(e["ctx"]["error"]) for e in exc.errors() if e.get("ctx",{}).get("error")]
        return JSONResponse({"detail":" ".join(messages) or "Bot sozlamalarini tekshiring: nom, USD kripto juftligi va risk qiymatlari ruxsat etilgan chegaralarda bo‘lsin."},status_code=422)
    return JSONResponse({"detail":"Maydonlarni tekshiring. Tezlik 1–10 Hz, aktivlar soni 1–6 bo‘lishi kerak."},status_code=422)


@app.exception_handler(BotError)
async def bot_error(request, exc):
    return JSONResponse({"detail":str(exc)},status_code=400)


@app.exception_handler(BrokerError)
async def broker_error(request, exc):
    return JSONResponse({"detail":str(exc)},status_code=400)


@app.middleware("http")
async def security(request: Request, call_next):
    if request.method not in ("GET","HEAD","OPTIONS"):
        if not secrets.compare_digest(request.headers.get("x-auto-trader-token", ""),TOKEN):
            return JSONResponse({"detail":"Sahifani yangilab qayta urinib ko‘ring."},status_code=403)
        origin = request.headers.get("origin")
        if origin and origin != str(request.base_url).rstrip("/"):
            return JSONResponse({"detail":"Begona manbadan so‘rov rad etildi."},status_code=403)
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/api/bootstrap")
async def bootstrap():
    return {"token":TOKEN, **runtime.snapshot()}


@app.post("/api/connection")
async def connect(payload: Connection):
    if not payload.api_key.get_secret_value().strip() or not payload.secret_key.get_secret_value().strip():
        raise HTTPException(422,"Ikkala kalitni ham kiriting.")
    async with runtime.lock:
        try:
            await runtime.connect(payload)
        except BrokerError as exc:
            raise HTTPException(400,str(exc)) from None
    return {"message":"Alpaca akkaunti ulandi.","mode":runtime.mode}


@app.delete("/api/connection")
async def disconnect():
    async with runtime.lock:
        if runtime.bots.busy():
            raise BotError("Faol bot, ochiq pozitsiya yoki kutilayotgan order bor. Avval Trader Bots bo‘limida ularni yakunlang.")
        try:
            forget_credentials(required=runtime.settings["remember"])
        except BrokerError as exc:
            raise HTTPException(400,str(exc)) from None
        await runtime.stop_broker()
        runtime.market, runtime.mode, runtime.account = Market(), "demo", None
        runtime.account_updated, runtime.account_error, runtime.startup_error = None, None, None
        runtime.settings.update(remember=False)
        runtime.settings["symbols"] = ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD"]
        runtime.save()
        runtime.analytics_time = 0
    return {"message":"Ulanish uzildi, saqlangan kalitlar o‘chirildi. Demo faol."}


@app.get("/api/bots")
async def list_bots():
    return {"bots":runtime.bots.public(),"error":runtime.bots.storage_error}


@app.post("/api/bots",status_code=201)
async def create_bot(payload: BotConfig):
    async with runtime.lock:
        bot = runtime.bots.create(payload)
    return {"id":bot["id"],"message":"Bot yaratildi. Hozircha savdo boshlanmagan."}


@app.put("/api/bots/{bot_id}")
async def update_bot(bot_id: str, payload: BotConfig):
    async with runtime.lock:
        runtime.bots.update(bot_id,payload)
    return {"message":"Bot sozlamalari saqlandi."}


@app.delete("/api/bots/{bot_id}")
async def delete_bot(bot_id: str):
    async with runtime.lock:
        runtime.bots.delete(bot_id)
    return {"message":"Bot o‘chirildi."}


@app.post("/api/bots/{bot_id}/start")
async def start_bot(bot_id: str, payload: BotAction):
    async with runtime.lock:
        await runtime.bots.start(bot_id,payload)
    return {"message":"Bot ishga tushdi. Yangi signal kutilmoqda."}


@app.post("/api/bots/{bot_id}/pause")
async def pause_bot(bot_id: str):
    async with runtime.lock:
        runtime.bots.pause(bot_id)
    return {"message":"Yangi xaridlar pauzada. Ochiq pozitsiyaning chiqish nazorati davom etadi."}


@app.post("/api/bots/{bot_id}/close")
async def close_bot_position(bot_id: str, payload: BotAction):
    async with runtime.lock:
        await runtime.bots.close_position(bot_id,payload)
    return {"message":"Pozitsiyani yopish so‘rovi qayta ishlandi. Order holatini tekshiring."}


@app.post("/api/bots/{bot_id}/backtest")
async def backtest_bot(bot_id: str):
    async with runtime.lock:
        return await runtime.bots.test(bot_id)


@app.get("/api/bots/{bot_id}/events")
async def bot_events(bot_id: str):
    return {"events":runtime.bots.events(bot_id)}


@app.put("/api/settings")
async def settings(payload: Settings):
    async with runtime.lock:
        symbols = list(dict.fromkeys(payload.symbols))
        available = {a["symbol"] for a in runtime.market.assets}
        if not set(symbols) <= available:
            raise HTTPException(422,"Tanlangan aktiv uchun ma’lumot manbasi ulanmagan.")
        runtime.settings.update(refresh_hz=payload.refresh_hz,symbols=symbols)
        runtime.save()
        runtime.analytics_time = 0
    return {"message":"Sozlamalar saqlandi."}


@app.get("/api/portfolio")
async def portfolio(period: Literal["1D","1W","1M","1A"]="1W"):
    async with runtime.lock:
        if runtime.broker:
            try:
                return {"demo":False,"points":await runtime.broker.history(period),"currency":runtime.account["currency"]}
            except BrokerError as exc:
                raise HTTPException(400,str(exc)) from None
    # A fixed illustrative series, independent of the simulated quote stream.
    import random
    rng = random.Random(17)
    count = 200
    span = {"1D":86400,"1W":604800,"1M":2592000,"1A":31536000}[period]
    v, points = 10000, []
    for i in range(count):
        v += rng.gauss(2,22)
        points.append({"time":time.time()-span+(span*i/(count-1)),"equity":round(v,2)})
    offset = 10000 - points[-1]["equity"]
    points = [{**p,"equity":round(p["equity"]+offset,2)} for p in points]
    return {"demo":True,"points":points,"currency":"USD"}


@app.websocket("/ws")
async def websocket(ws: WebSocket):
    origin = ws.headers.get("origin", "")
    host = ws.headers.get("host", "")
    if origin != f"http://{host}" or host.split(":")[0] not in ("127.0.0.1","localhost"):
        await ws.close(code=1008)
        return
    await ws.accept()
    try:
        auth = await asyncio.wait_for(ws.receive_json(), timeout=5)
        if not secrets.compare_digest(str(auth.get("token", "")), TOKEN):
            await ws.close(code=1008)
            return
        while True:
            await ws.send_json(runtime.snapshot())
            await asyncio.sleep(1/runtime.settings["refresh_hz"])
    except (WebSocketDisconnect, asyncio.TimeoutError, RuntimeError):
        pass


@app.get("/")
async def index():
    return FileResponse(ROOT / "static" / "index.html")


app.mount("/static",StaticFiles(directory=ROOT / "static"),name="static")
