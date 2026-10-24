"""Persistent bot ledger. Broker orders are only placed after an explicit start/close action.

All methods are called under Runtime.lock. An order intent is committed BEFORE POST;
an ambiguous response is looked up by client_order_id and is never blindly retried.
"""
import asyncio
import hashlib
import json
import math
import sqlite3
import time
import uuid
from contextlib import contextmanager
from decimal import Decimal, ROUND_DOWN
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .alpaca import BrokerError
from .strategies import FEE_RATE, MINUTES, backtest, bars_fresh, closed_bars, position_size, signal, pulse_signal


class BotError(Exception):
    pass


class BotConfig(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    name: str = Field(min_length=2,max_length=48)
    symbol: str = Field(pattern=r"^[A-Z0-9]+/USD$")
    mode: Literal["paper","live"] = "paper"
    strategy: Literal["trend_guard","pulse_micro"] = "trend_guard"
    timeframe: Literal["5Min","15Min","1Hour"] = "15Min"
    budget_usd: float = Field(default=100,ge=20,le=100000)
    max_position_usd: float = Field(default=25,ge=5,le=100000)
    risk_pct: float = Field(default=.5,ge=.1,le=2)
    stop_loss_pct: float = Field(default=2,ge=.5,le=10)
    take_profit_pct: float = Field(default=4,ge=1,le=30)
    daily_loss_pct: float = Field(default=3,ge=1,le=10)
    max_daily_trades: int = Field(default=4,ge=1,le=7200)
    cooldown_bars: int = Field(default=3,ge=1,le=12)
    max_spread_pct: float = Field(default=.5,ge=.05,le=2)

    max_orders_per_minute: int = Field(default=3,ge=1,le=5)
    cooldown_seconds: int = Field(default=12,ge=12,le=3600)
    hourly_profit_target_pct: float = Field(default=0,ge=0,le=100)
    momentum_threshold_pct: float = Field(default=.1,ge=.02,le=5)

    @field_validator("name",mode="before")
    @classmethod
    def clean_name(cls,value):
        if not isinstance(value,str) or any(ord(c)<32 for c in value):
            raise ValueError("Nom oddiy matn bo‘lishi kerak.")
        return value.strip()

    @model_validator(mode="after")
    def limits(self):
        if self.strategy=="trend_guard" and self.max_daily_trades>12:
            raise ValueError("Trend Guard uchun kunlik kirishlar 12 dan oshmasin.")
        if self.max_position_usd>self.budget_usd:
            raise ValueError("Pozitsiya limiti bot budjetidan oshmasin.")
        if self.take_profit_pct<=self.stop_loss_pct:
            raise ValueError("Take-profit foizi stop-loss foizidan katta bo‘lsin.")
        if position_size(self.model_dump(),self.budget_usd)<5:
            raise ValueError("Budjet va risk juda kichik: hisoblangan order kamida $5 bo‘lsin.")
        return self


class BotAction(BaseModel):
    acknowledge_risk: bool = False
    confirmation: str = ""
    review_token: str = ""


def review_token(bot):
    return hashlib.sha256(json.dumps(bot["config"],sort_keys=True).encode()).hexdigest()


def account_ref(account, mode):
    if not account or not account.get("id"):
        raise BotError("Akkaunt identifikatori olinmadi. Alpaca’ni qayta ulang.")
    return hashlib.sha256(f"{mode}:{account['id']}".encode()).hexdigest()[:32]


def normalized(symbol):
    return symbol.replace("/", "").upper()


def floor_qty(qty,increment):
    step = Decimal(str(increment))
    value = Decimal(str(qty))
    if not step.is_finite() or step<=0 or not value.is_finite() or value<0:
        raise BotError("Aktivning miqdor qadami noto‘g‘ri.")
    return format((value/step).to_integral_value(rounding=ROUND_DOWN)*step,"f")


class BotEngine:
    def __init__(self, runtime, path):
        self.runtime, self.path = runtime, Path(path)
        self.items = {}
        self.bars_cache = {}
        self.storage_error = None
        self.lock_file = None
        if self.path.exists():
            try:
                with self.db() as db:
                    for row in db.execute("SELECT payload FROM bots"):
                        bot = json.loads(row[0])
                        bot["config"] = BotConfig(**bot["config"]).model_dump()
                        bot.setdefault("order_times",[])
                        bot.setdefault("hour",None)
                        bot.setdefault("hour_start",bot["config"]["budget_usd"])
                        bot.setdefault("halted_hour",None)
                        if bot["status"]=="running" or bot["qty"]>0 or bot["pending"]:
                            bot.update(status="paused",requires_resume=True,message="Server qayta ishga tushdi. Orderlar tekshiriladi; savdoni qo‘lda davom ettiring.")
                        self.items[bot["id"]] = bot
            except (OSError,sqlite3.Error,ValueError,KeyError,TypeError):
                self.storage_error = "Botlar bazasini o‘qib bo‘lmadi. Savdo bloklandi; bazani tiklang."

    def acquire(self):
        """Only one bot engine may own this data directory across server processes."""
        self.path.parent.mkdir(parents=True,exist_ok=True)
        lock = self.path.with_suffix(".lock").open("a+b")
        try:
            lock.seek(0)
            if __import__("os").name=="nt":
                import msvcrt
                if not lock.read(1):
                    lock.write(b"0")
                    lock.flush()
                lock.seek(0)
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.lock_file = lock
        except OSError:
            lock.close()
            self.storage_error = "Bu ma’lumot papkasida boshqa bot serveri ishlamoqda. Ikkinchi savdo jarayoni bloklandi."

    def release(self):
        if self.lock_file:
            self.lock_file.close()
            self.lock_file = None

    @contextmanager
    def db(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        db = sqlite3.connect(self.path,timeout=5)
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS bots (id TEXT PRIMARY KEY,payload TEXT NOT NULL)")
        db.execute("CREATE TABLE IF NOT EXISTS events (seq INTEGER PRIMARY KEY AUTOINCREMENT,bot_id TEXT,time REAL,message TEXT)")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, bot, event=None):
        if self.storage_error:
            raise BotError(self.storage_error)
        try:
            with self.db() as db:
                db.execute("INSERT OR REPLACE INTO bots VALUES (?,?)",(bot["id"],json.dumps(bot,allow_nan=False)))
                if event:
                    db.execute("INSERT INTO events(bot_id,time,message) VALUES (?,?,?)",(bot["id"],time.time(),event))
                    db.execute("DELETE FROM events WHERE bot_id=? AND seq NOT IN (SELECT seq FROM events WHERE bot_id=? ORDER BY seq DESC LIMIT 200)",(bot["id"],bot["id"]))
        except (OSError,sqlite3.Error,ValueError):
            self.storage_error = "Bot jurnaliga yozib bo‘lmadi. Yangi orderlar bloklandi. Diskni tekshiring."
            raise BotError(self.storage_error) from None

    def say(self, bot, message):
        if bot["message"]!=message:
            bot["message"] = message
            self.save(bot,message)

    def get(self, bot_id):
        if bot_id not in self.items:
            raise BotError("Bot topilmadi.")
        return self.items[bot_id]

    def validate_config(self, config, exclude=None):
        if self.storage_error:
            raise BotError(self.storage_error)
        if len(self.items)>=10 and exclude is None:
            raise BotError("Bir dasturda ko‘pi bilan 10 ta bot yarating.")
        assets = self.runtime.market.assets
        if config.symbol not in {a["symbol"] for a in assets}:
            raise BotError("Bu kripto joriy Alpaca aktivlar ro‘yxatida yo‘q.")
        for bot in self.items.values():
            if bot["id"]!=exclude and bot["config"]["name"].casefold()==config.name.casefold():
                raise BotError("Bu nom bilan bot mavjud. Boshqa nom tanlang.")

    def create(self, config):
        self.validate_config(config)
        bot = {"id":uuid.uuid4().hex,"config":config.model_dump(),"status":"draft","owner":None,"requires_resume":False,
               "created_at":time.time(),"message":"Bot yaratildi. Ishga tushirishni kutmoqda.","qty":0.,"cost":0.,
               "realized":0.,"fee_estimate":0.,"pending":None,"sequence":0,"last_bar":0.,"last_exit":0.,
               "order_times":[],"hour":None,"hour_start":config.budget_usd,"halted_hour":None,"day":None,"day_start":config.budget_usd,"day_entries":0,"halted_day":None,
               "filled_orders":0,"signal":None,"backtest":None,"retry_after":0.,"dust_qty":0.}
        self.save(bot,"Bot yaratildi; order yuborilmadi.")
        self.items[bot["id"]] = bot
        return bot

    def update(self, bot_id, config):
        bot = self.get(bot_id)
        if bot["status"]=="running" or bot["qty"]>0 or bot["pending"]:
            raise BotError("Sozlamalarni o‘zgartirish uchun botni pauza qiling va uning pozitsiyasini yoping.")
        self.validate_config(config,exclude=bot_id)
        if config.mode!=bot["config"]["mode"] or config.symbol!=bot["config"]["symbol"]:
            raise BotError("Muhit yoki kriptoni almashtirish uchun yangi bot yarating.")
        bot.update(config=config.model_dump(),backtest=None,day=None,halted_day=None,hour=None,halted_hour=None)
        self.save(bot,"Bot sozlamalari yangilandi.")

    def delete(self, bot_id):
        if self.storage_error:
            raise BotError(self.storage_error)
        bot = self.get(bot_id)
        if bot["status"]=="running" or bot["qty"]>0 or bot["pending"]:
            raise BotError("Faol, ochiq pozitsiyali yoki kutilayotgan orderli botni o‘chirib bo‘lmaydi.")
        with self.db() as db:
            db.execute("DELETE FROM bots WHERE id=?",(bot_id,))
            db.execute("DELETE FROM events WHERE bot_id=?",(bot_id,))
        del self.items[bot_id]

    def events(self, bot_id):
        self.get(bot_id)
        with self.db() as db:
            return [{"time":t,"message":m} for t,m in db.execute("SELECT time,message FROM events WHERE bot_id=? ORDER BY seq DESC LIMIT 50",(bot_id,))]

    def busy(self):
        return [b for b in self.items.values() if b["status"]=="running" or b["qty"]>0 or b["pending"]]

    def check_switch(self, account, mode):
        if self.busy():
            ref = account_ref(account,mode)
            if any(b["owner"]!=ref for b in self.busy()):
                raise BotError("Botda ochiq pozitsiya yoki order bor. Boshqa akkauntga o‘tishdan oldin uni yakunlang.")

    def public(self):
        result = []
        for bot in self.items.values():
            q = self.runtime.market.quotes.get(bot["config"]["symbol"],{})
            same = self.runtime.mode==bot["config"]["mode"] and self.runtime.account and self.runtime.account.get("id")
            if same:
                same = bot["owner"] in (None,account_ref(self.runtime.account,self.runtime.mode))
            price = q.get("bid",q.get("price")) if same else None
            unrealized = bot["qty"]*price*(1-FEE_RATE)-bot["cost"] if bot["qty"] and price else None
            result.append({**{k:v for k,v in bot.items() if k not in ("owner","backtest")},
                           "hourly_pnl":(bot["config"]["budget_usd"]+bot["realized"]+(unrealized or 0)-bot["hour_start"]) if bot["hour"]==int(self.runtime.market.now()//3600) and (not bot["qty"] or unrealized is not None) else None,
                           "orders_last_minute":len([t for t in bot["order_times"] if self.runtime.market.now()-60<t<=self.runtime.market.now()]),
                           "estimated_unrealized":unrealized,"estimated_total":bot["realized"]+(unrealized or 0),
                           "quote_stale":not same or self.runtime.market.quote_age(q)>15,
                           "backtest":{k:v for k,v in bot["backtest"].items() if k not in ("curve","recent_trades")} if bot["backtest"] else None,
                           "account_matches":bool(same),"review_token":review_token(bot)})
        return sorted(result,key=lambda b:b["created_at"],reverse=True)

    def ensure_broker(self, bot):
        rt = self.runtime
        if not rt.broker or rt.mode!=bot["config"]["mode"]:
            raise BotError(f"Account bo‘limida {bot['config']['mode'].upper()} kalitlarini ulang.")
        ref = account_ref(rt.account,rt.mode)
        if bot["owner"] and bot["owner"]!=ref:
            raise BotError("Bu bot boshqa Alpaca akkauntiga tegishli.")
        return rt.broker,ref

    def confirm(self,bot,action,verb="LIVE"):
        if not action.acknowledge_risk:
            raise BotError("Budjet, risklar va lokal stop-loss shartlarini tasdiqlang.")
        if action.review_token!=review_token(bot):
            raise BotError("Bot sozlamalari o‘zgargan. Oynani yopib, yangilangan limitlarni qayta ko‘rib chiqing.")
        if bot["config"]["mode"]=="live" and action.confirmation!=f"{verb} {bot['config']['name']}":
            raise BotError(f"Tasdiqlash maydoniga {verb} {bot['config']['name']} deb yozing.")

    def asset(self,bot):
        a = next((a for a in self.runtime.market.assets if a["symbol"]==bot["config"]["symbol"] and a.get("tradable")),None)
        if not a or not a.get("min_order_size") or not a.get("min_trade_increment"):
            raise BotError("Savdo uchun aktivning minimal miqdori va qadami olinmadi. Alpaca’ni qayta ulang.")
        return a

    def position(self,positions,bot):
        return next((p for p in positions if normalized(p["symbol"])==normalized(bot["config"]["symbol"])),None)

    async def start(self,bot_id,action):
        bot = self.get(bot_id)
        self.confirm(bot,action)
        broker,ref = self.ensure_broker(bot)
        self.asset(bot)
        if bot["pending"]:
            raise BotError("Avval kutilayotgan order holati aniqlanishi kerak.")
        if bot["status"]=="running":
            return
        if any(b["id"]!=bot_id and b["owner"]==ref and b["config"]["symbol"]==bot["config"]["symbol"] and (b["status"]=="running" or b["qty"]>0 or b["pending"]) for b in self.items.values()):
            raise BotError("Bu kriptoda boshqa bot faol. Bitta kriptoga faqat bitta faol bot ruxsat etiladi.")
        account,positions,orders = await asyncio.gather(broker.account(),broker.positions(),broker.open_orders())
        if account_ref(account,self.runtime.mode)!=ref or account.get("status")!="ACTIVE" or account.get("trading_blocked") or account.get("account_blocked") or account.get("currency")!="USD":
            raise BotError("Akkaunt savdoga tayyor emas yoki uning valyutasi USD emas.")
        position = self.position(positions,bot)
        actual = float(position.get("qty",0)) if position else 0
        if (bot["qty"]==0 and actual>bot["dust_qty"]+1e-12) or (bot["qty"]>0 and actual<bot["qty"]*(1-FEE_RATE-.001)):
            raise BotError("Kriptoda bot jurnaliga mos kelmaydigan pozitsiya bor. Qo‘lda ochilgan pozitsiyalarni bot boshqarmaydi.")
        if any(normalized(o["symbol"])==normalized(bot["config"]["symbol"]) for o in orders):
            raise BotError("Bu kriptoda allaqachon ochiq order bor. Avval uning yakunlanishini kuting.")
        reserved = sum(max(0,b["config"]["budget_usd"]+b["realized"]-b["cost"]) for b in self.items.values() if b["id"]!=bot_id and b["owner"]==ref and (b["status"]=="running" or b["qty"]>0 or b["pending"]))
        needed = max(0,bot["config"]["budget_usd"]+bot["realized"]-bot["cost"])
        if needed+reserved>float(account["cash"]):
            raise BotError("Naqd balans boshqa faol botlar budjeti bilan birga yetarli emas. Budjetni kamaytiring.")
        if bot["config"]["strategy"]=="pulse_micro":
            self.quote(bot)
            bot.update(owner=ref,status="running",requires_resume=False,retry_after=0.)
            self.save(bot,"Pulse Micro ishga tushdi. Signal va risk filtrlari tekshiriladi; savdo soni kafolatlanmaydi.")
            return
        bars = await self.bars(bot,force=True)
        if len(bars)<100 or not bars_fresh(bars,bot["config"]["timeframe"],self.runtime.market.now()):
            raise BotError("Boshlash uchun kamida 100 ta yangi yopilgan sham kerak.")
        bot.update(owner=ref,status="running",requires_resume=False,last_bar=0. if bot["qty"]>0 else bars[-1]["time"],retry_after=0.)
        self.say(bot,"Bot ishga tushdi. Keyingi yopilgan shamdagi yangi signal kutilmoqda.")
        self.save(bot)

    def pause(self, bot_id):
        bot = self.get(bot_id)
        bot["status"] = "paused"
        self.say(bot,"Yangi xaridlar pauzada. Ochiq pozitsiyaning avtomatik chiqish nazorati server ishlaganda davom etadi.")
        self.save(bot)

    async def bars(self, bot, force=False, limit=350):
        broker,_ = self.ensure_broker(bot)
        c = bot["config"]
        key = (bot["owner"],c["symbol"],c["timeframe"],limit)
        cached = self.bars_cache.get(key)
        if not force and cached and time.time()-cached[0]<20:
            return cached[1]
        bars = closed_bars(await broker.bars(c["symbol"],c["timeframe"],limit),c["timeframe"],self.runtime.market.now())
        self.bars_cache[key] = (time.time(),bars)
        return bars

    async def test(self,bot_id):
        bot = self.get(bot_id)
        if not self.runtime.broker:
            raise BotError("Backtest uchun Account bo‘limida Alpaca’ni ulang.")
        config = bot["config"]
        if config["strategy"]=="pulse_micro":
            raise BotError("Pulse Micro uchun sekundlik tick tarixi kerak. Sham backtesti mos emas; Paper forward sinovdan foydalaning.")
        raw = await self.runtime.broker.bars(config["symbol"],config["timeframe"],1000)
        now = self.runtime.broker.market_time() if hasattr(self.runtime.broker,"market_time") else time.time()
        bars = closed_bars(raw,config["timeframe"],now)
        if len(bars)<150:
            raise BotError("Backtest uchun kamida 150 ta yopilgan sham kerak.")
        bot["backtest"] = backtest(bars,bot["config"])
        self.save(bot,"Tarixiy backtest bajarildi; brokerga order yuborilmadi.")
        return bot["backtest"]

    async def close_position(self,bot_id,action):
        bot = self.get(bot_id)
        self.confirm(bot,action,verb="CLOSE")
        self.ensure_broker(bot)
        if bot["pending"]:
            raise BotError("Kutilayotgan order yakunlanishini kuting.")
        if bot["qty"]<=0:
            raise BotError("Botning ochiq pozitsiyasi yo‘q.")
        bot["status"] = "paused"
        bot["requires_resume"] = False
        await self.exit(bot,"Foydalanuvchi pozitsiyani yopishni so‘radi.")

    async def submit(self,bot,side,qty,reason):
        broker,_ = self.ensure_broker(bot)
        if self.storage_error:
            raise BotError(self.storage_error)
        now = self.runtime.market.now()
        bot["order_times"] = [t for t in bot["order_times"] if t>now-60]
        if side=="buy" and bot["config"]["strategy"]=="pulse_micro":
            if len(bot["order_times"])>=bot["config"]["max_orders_per_minute"] or (bot["order_times"] and now-bot["order_times"][-1]<60/bot["config"]["max_orders_per_minute"]):
                self.say(bot,"Daqiqalik order limiti: yangi xarid kutilmoqda. Himoya sotuvlari bloklanmaydi.")
                return
        bot["order_times"].append(now)
        bot["sequence"] += 1
        cid = f"at-{bot['id'][:20]}-{bot['sequence']}"
        bot["pending"] = {"client_order_id":cid,"side":side,"qty":qty,"created_at":time.time(),"applied_qty":0.,"applied_gross":0.,"order_id":None,"cancel_sent":False}
        if side=="buy":
            bot["day_entries"] += 1
        self.save(bot,f"{side.upper()} order niyati saqlandi: {qty} {bot['config']['symbol']}. {reason}")
        try:
            order = await broker.submit_order(bot["config"]["symbol"],side,qty,cid)
        except BrokerError as exc:
            if not exc.uncertain and exc.status and 400<=exc.status<500 and exc.status!=429:
                bot.update(pending=None,status="error",retry_after=time.time()+60)
                self.say(bot,"Order rad etildi. Budjet, minimal miqdor va akkaunt ruxsatlarini tekshiring.")
                self.save(bot)
            else:
                self.say(bot,"Order javobi noaniq. Client ID orqali tekshiriladi; takror yuborilmaydi.")
            return
        self.apply_order(bot,order)

    def apply_order(self,bot,order):
        p = bot["pending"]
        if not p:
            return
        if order.get("client_order_id")!=p["client_order_id"] or order.get("side")!=p["side"] or normalized(order.get("symbol",""))!=normalized(bot["config"]["symbol"]):
            raise BotError("Order identifikatori jurnalga mos emas. Savdo to‘xtatildi.")
        qty = float(order.get("filled_qty") or 0)
        average = float(order.get("filled_avg_price") or 0)
        if not all(math.isfinite(v) for v in (qty,average)) or qty<0 or average<0 or (qty>0 and average<=0) or qty+1e-12<p["applied_qty"] or qty>float(p["qty"])*(1+1e-9):
            raise BotError("Order fill ma’lumotlari noto‘g‘ri; qo‘lda tekshirish kerak.")
        dq,dgross = qty-p["applied_qty"],qty*average-p["applied_gross"]
        if p["side"]=="buy":
            bot["qty"] += dq
            bot["cost"] += dgross*(1+FEE_RATE)
        elif dq>0:
            if dq>bot["qty"]+1e-9:
                raise BotError("Fill botga tegishli miqdordan oshdi.")
            fraction = min(1,dq/bot["qty"]) if bot["qty"] else 0
            basis = bot["cost"]*fraction
            bot["qty"] = max(0,bot["qty"]-dq)
            bot["cost"] -= basis
            bot["realized"] += dgross*(1-FEE_RATE)-basis
        elif p["side"]=="sell" and dgross:
            bot["realized"] += dgross*(1-FEE_RATE)
        bot["fee_estimate"] += dgross*FEE_RATE
        p.update(applied_qty=qty,applied_gross=qty*average,order_id=order.get("id"))
        status = order.get("status")
        if status in ("filled","canceled","expired","rejected"):
            if qty>0:
                bot["filled_orders"] += 1
            if p["side"]=="sell" and qty>0:
                bot["last_exit"] = time.time()
                # Fee debits may be in crypto and are posted later. Record small residuals.
                if bot["qty"]<=max(qty*(FEE_RATE+.001),1e-12):
                    bot["dust_qty"] += bot["qty"]
                    bot["realized"] -= bot["cost"]
                    bot["qty"],bot["cost"] = 0.,0.
            bot["pending"] = None
            bot["message"] = f"Order {status}. Bajarilgan miqdor: {qty:g}."
            self.save(bot,bot["message"])
        else:
            bot["message"] = f"Order: {status or 'kutilmoqda'}. Bajarilgan miqdor: {qty:g}."
            self.save(bot)

    async def reconcile(self,bot):
        broker,_ = self.ensure_broker(bot)
        p = bot["pending"]
        if not p:
            return
        try:
            order = await broker.order_by_client_id(p["client_order_id"])
        except BrokerError as exc:
            if exc.status==404:
                self.say(bot,"Order hali topilmadi. Avtomatik qayta yuborish bloklangan; Alpaca’da Client ID orqali tekshiring.")
                return
            raise
        self.apply_order(bot,order)
        p = bot["pending"]
        if p and p["order_id"] and time.time()-p["created_at"]>45 and not p["cancel_sent"] and not bot["requires_resume"]:
            await broker.cancel_order(p["order_id"])
            p["cancel_sent"] = True
            self.save(bot,"45 soniyadan ortiq ochiq order uchun bekor qilish so‘rovi yuborildi. Yakuniy holat kutilmoqda.")

    def quote(self,bot):
        q = self.runtime.market.quotes.get(bot["config"]["symbol"],{})
        if (not all(math.isfinite(q.get(k,0)) and q.get(k,0)>0 for k in ("price","bid","ask"))
                or q.get("ask",0)<q.get("bid",0) or not -30<=self.runtime.market.quote_age(q)<=15):
            raise BotError("Yangi bid/ask kotirovkasi kutilmoqda; eskirgan narxda order yuborilmaydi.")
        return q

    async def exit(self,bot,reason):
        broker,_ = self.ensure_broker(bot)
        self.quote(bot)
        asset = self.asset(bot)
        position = self.position(await broker.positions(),bot)
        actual = max(0,float(position.get("qty_available",position.get("qty",0)))) if position else 0
        if actual<bot["qty"]*(1-FEE_RATE-.001):
            bot.update(status="error",requires_resume=True)
            self.say(bot,"Broker pozitsiyasi jurnalga mos kelmaydi. Qo‘lda o‘zgarish yoki kamomad bor; order yuborilmadi.")
            return
        qty = floor_qty(min(bot["qty"],actual),asset["min_trade_increment"])
        if float(qty)<float(asset["min_order_size"]):
            bot.update(status="error",requires_resume=True)
            self.say(bot,"Qolgan pozitsiya minimal orderdan kichik. Alpaca’da qoldiqni tekshiring.")
            return
        open_orders = await broker.open_orders()
        if any(normalized(o["symbol"])==normalized(bot["config"]["symbol"]) for o in open_orders):
            raise BotError("Bu kriptoda tashqi ochiq order bor. Takroriy order yuborilmadi.")
        await self.submit(bot,"sell",qty,reason)

    async def tick(self,bot):
        self.ensure_broker(bot)
        if bot["pending"]:
            await self.reconcile(bot)
            return
        if bot["requires_resume"] or time.time()<bot["retry_after"]:
            return
        if bot["status"]!="running" and bot["qty"]<=0:
            return
        q = self.quote(bot)
        c = bot["config"]
        equity = c["budget_usd"]+bot["realized"]+(bot["qty"]*q["bid"]*(1-FEE_RATE)-bot["cost"] if bot["qty"] else 0)
        hour = int(self.runtime.market.now()//3600)
        if bot["hour"]!=hour:
            bot.update(hour=hour,hour_start=equity,halted_hour=None)
            self.save(bot)
        if c["hourly_profit_target_pct"]>0 and equity-bot["hour_start"]>=c["budget_usd"]*c["hourly_profit_target_pct"]/100:
            bot["halted_hour"] = hour
            self.save(bot)
        day = int(self.runtime.market.now()//86400)
        if bot["day"]!=day:
            bot.update(day=day,day_start=equity,day_entries=0,halted_day=None)
            self.save(bot)
        if equity<=bot["day_start"]-c["budget_usd"]*c["daily_loss_pct"]/100:
            bot["halted_day"] = day
            self.save(bot)
        if bot["qty"]:
            entry = bot["cost"]/(bot["qty"]*(1+FEE_RATE))
            reason = "Kunlik zarar limiti" if bot["halted_day"]==day else "Stop-loss" if q["bid"]<=entry*(1-c["stop_loss_pct"]/100) else "Take-profit" if q["bid"]>=entry*(1+c["take_profit_pct"]/100) else "Soatlik foyda maqsadiga yetildi" if bot["halted_hour"]==hour else None
            if reason:
                await self.exit(bot,reason)
                return
        if bot["halted_day"]==day:
            self.say(bot,"Kunlik zarar limiti: yangi xaridlar keyingi UTC kungacha bloklandi.")
            return
        if bot["halted_hour"]==hour:
            self.say(bot,"Soatlik maqsadga yetildi: yangi xaridlar keyingi UTC soatigacha bloklandi. Yakuniy foyda fill va komissiyaga bog‘liq.")
            return
        if c["strategy"]=="pulse_micro":
            decision = pulse_signal(self.runtime.market.series,c["symbol"],self.runtime.market.now(),c["momentum_threshold_pct"])
        else:
            decision = await self.trend_decision(bot)
            if decision is None:
                return
        bot["signal"] = decision
        self.save(bot)
        if bot["qty"]:
            if decision["action"]=="sell":
                await self.exit(bot,decision["reason"])
            return
        if bot["status"]!="running":
            return
        if decision["action"]!="buy":
            self.say(bot,decision["reason"])
            return
        if bot["day_entries"]>=c["max_daily_trades"] or time.time()-bot["last_exit"]<(c["cooldown_seconds"] if c["strategy"]=="pulse_micro" else c["cooldown_bars"]*MINUTES[c["timeframe"]]*60):
            self.say(bot,"Savdolar limiti yoki cooldown: xarid o‘tkazib yuborildi.")
            return
        if (q["ask"]-q["bid"])/q["price"]*100>c["max_spread_pct"]:
            self.say(bot,"Spread limitdan katta. Xarid o‘tkazib yuborildi.")
            return
        broker,_ = self.ensure_broker(bot)
        account,positions,orders = await asyncio.gather(broker.account(),broker.positions(),broker.open_orders())
        if account.get("trading_blocked") or account.get("account_blocked") or account.get("status")!="ACTIVE":
            raise BotError("Akkauntda savdo bloklangan.")
        existing = self.position(positions,bot)
        if existing and float(existing.get("qty",0))>bot["dust_qty"]+1e-12:
            raise BotError("Botdan tashqari pozitsiya bor. Xarid yuborilmadi.")
        if any(normalized(o["symbol"])==normalized(c["symbol"]) for o in orders):
            raise BotError("Bu kriptoda tashqi ochiq order bor. Xarid yuborilmadi.")
        cash = min(max(0,c["budget_usd"]+bot["realized"]),max(0,float(account["cash"])*.95))
        notional = position_size(c,cash)
        asset = self.asset(bot)
        qty = floor_qty(notional/q["ask"],asset["min_trade_increment"])
        if float(qty)<float(asset["min_order_size"]) or float(qty)*q["ask"]<5:
            self.say(bot,"Risk budjeti yoki balans minimal order uchun yetarli emas.")
            return
        await self.submit(bot,"buy",qty,decision["reason"])

    async def trend_decision(self,bot):
        c = bot["config"]
        bars = await self.bars(bot)
        if len(bars)<100 or not bars_fresh(bars,c["timeframe"],self.runtime.market.now()):
            raise BotError("Yetarli yoki yangi yopilgan shamlar yo‘q. Savdo kutilmoqda.")
        if bars[-1]["time"]<=bot["last_bar"]:
            return None
        bot["last_bar"] = bars[-1]["time"]
        return signal(bars)

    async def run(self):
        while True:
            if self.runtime.broker and not self.storage_error:
                for bot in list(self.items.values()):
                    async with self.runtime.lock:
                        if bot["id"] not in self.items or not self.runtime.broker or self.storage_error:
                            continue
                        if bot["status"]!="running" and bot["qty"]<=0 and not bot["pending"]:
                            continue
                        try:
                            await self.tick(bot)
                        except (BotError,BrokerError) as exc:
                            try:
                                self.say(bot,str(exc))
                            except BotError:
                                break
                        except asyncio.CancelledError:
                            raise
                        except Exception:
                            bot.update(status="error",requires_resume=True)
                            try:
                                self.say(bot,"Botda kutilmagan xato. Savdo pauzada; jurnal va Alpaca orderlarini tekshiring.")
                            except BotError:
                                break
            await asyncio.sleep(5)
