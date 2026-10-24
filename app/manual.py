"""Reviewed manual orders with a durable intent and no automatic POST retries."""
import asyncio
import json
import math
import uuid

from pydantic import BaseModel, ConfigDict, Field, model_validator
from typing import Literal

from .alpaca import BrokerError
from .bots import BotError, account_ref, normalized, floor_qty
from .strategies import FEE_RATE

TERMINAL = {"filled", "canceled", "expired", "rejected"}
UNRESOLVED = {"submitted", "uncertain"}


class ManualTicket(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    symbol: str = Field(pattern=r"^[A-Z0-9]+/USD$")
    side: Literal["buy", "sell"]
    amount: float = Field(gt=0, le=1e18)
    mode: Literal["paper", "live"]


    @model_validator(mode="after")
    def amount_limit(self):
        if self.side=="buy" and self.amount>100000:
            raise ValueError("Bitta qo‘lda xarid $100,000 dan oshmasin.")
        return self


class ManualConfirm(BaseModel):
    acknowledge_risk: bool = False
    confirmation: str = ""


class ManualTrading:
    def __init__(self, runtime):
        self.rt = runtime

    def rows(self):
        if self.rt.bots.storage_error:
            raise BotError(self.rt.bots.storage_error)
        with self.rt.bots.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS manual_orders (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            return [json.loads(r[0]) for r in db.execute("SELECT payload FROM manual_orders")]

    def save(self, row):
        # Persist before contacting the broker. Disk failure must prevent POST.
        self.rows()
        with self.rt.bots.db() as db:
            db.execute("INSERT OR REPLACE INTO manual_orders VALUES (?,?)", (row["id"], json.dumps(row, allow_nan=False)))

    def broker(self):
        if not self.rt.broker or self.rt.mode not in ("paper", "live"):
            raise BotError("Qo‘lda savdo uchun Account bo‘limida Alpaca Paper yoki Live akkauntini ulang.")
        return self.rt.broker, account_ref(self.rt.account, self.rt.mode)

    def pending(self, symbol=None):
        return [r for r in self.rows() if r["status"] in UNRESOLVED and (symbol is None or r["symbol"]==symbol)]

    def check_switch(self, account, mode):
        ref = account_ref(account, mode) if self.pending() else None
        if any(r["owner"]!=ref for r in self.pending()):
            raise BotError("Qo‘lda yuborilgan order holati noaniq. Akkauntni almashtirishdan oldin tekshiring.")

    def check_bot(self, symbol):
        if any(r["mode"]==self.rt.mode for r in self.pending(symbol)):
            raise BotError("Bu kriptoda qo‘lda yuborilgan order yakunlanmagan. Qo‘lda savdo bo‘limida holatini tekshiring.")

    async def validate(self, ticket, qty=None, exclude=None):
        broker, ref = self.broker()
        if ticket.mode!=self.rt.mode:
            raise BotError("Savdo muhiti o‘zgardi. Orderni qayta ko‘rib chiqing.")
        if any(b["owner"]==ref and b["config"]["symbol"]==ticket.symbol and (b["status"]=="running" or b["qty"]>0 or b["pending"]) for b in self.rt.bots.items.values()):
            raise BotError("Bu kripto bot tomonidan boshqarilmoqda. Qo‘lda savdo uchun botni pauza qiling va bot pozitsiyasini uning bo‘limida yoping.")
        if any(r["owner"]==ref and r["id"]!=exclude for r in self.pending()):
            raise BotError("Avval yuborilgan qo‘lda order holatini tekshiring. Takroriy order bloklandi.")
        asset = next((a for a in self.rt.market.assets if a["symbol"]==ticket.symbol and a.get("tradable")), None)
        if not asset or not asset.get("min_order_size") or not asset.get("min_trade_increment"):
            raise BotError("Aktiv savdoga tayyor emas yoki minimal miqdor ma’lumoti yo‘q.")
        q = self.rt.bots.quote({"config":{"symbol":ticket.symbol}})
        if (q["ask"]-q["bid"])/q["price"]>.02:
            raise BotError("Spread 2% dan katta. Order yuborilmadi.")
        account, positions, orders = await asyncio.gather(broker.account(), broker.positions(), broker.open_orders())
        if account_ref(account, self.rt.mode)!=ref or account.get("status")!="ACTIVE" or account.get("currency")!="USD" or account.get("trading_blocked") or account.get("account_blocked"):
            raise BotError("Akkaunt savdoga tayyor emas.")
        if any(normalized(o["symbol"])==normalized(ticket.symbol) for o in orders):
            raise BotError("Bu kriptoda ochiq order bor. Avval uning yakunlanishini kuting.")
        q = self.rt.bots.quote({"config":{"symbol":ticket.symbol}})
        if (q["ask"]-q["bid"])/q["price"]>.02:
            raise BotError("Spread 2% dan katta. Order yuborilmadi.")
        if not math.isfinite(float(account["cash"])):
            raise BotError("Balans ma’lumoti noto‘g‘ri.")
        price = q["ask"] if ticket.side=="buy" else q["bid"]
        raw_qty = ticket.amount/price if ticket.side=="buy" else ticket.amount
        quantity = floor_qty(raw_qty if qty is None else qty, asset["min_trade_increment"])
        value = float(quantity)*price
        if not math.isfinite(value) or float(quantity)<float(asset["min_order_size"]) or value<5:
            raise BotError("Order minimal miqdorga mos va kamida $5 bo‘lsin.")
        if ticket.side=="buy":
            reserved = sum(max(0,b["config"]["budget_usd"]+b["realized"]-b["cost"]) for b in self.rt.bots.items.values() if b["owner"]==ref and (b["status"]=="running" or b["qty"]>0 or b["pending"]))
            if value*(1+FEE_RATE+.01)>max(0,float(account["cash"])-reserved):
                raise BotError("Erkin naqd balans yetmaydi. Botlar budjeti, komissiya va 1% narx zaxirasi hisobga olinadi.")
        else:
            pos = next((p for p in positions if normalized(p["symbol"])==normalized(ticket.symbol)), {})
            available = float(pos.get("qty_available",pos.get("qty",0)))
            if not math.isfinite(available) or float(quantity)>available+1e-12:
                raise BotError("Sotish uchun erkin kripto miqdori yetmaydi. Short savdo qo‘llanmaydi.")
        return ref, quantity, price

    async def preview(self, ticket):
        ref, qty, price = await self.validate(ticket)
        now = self.rt.market.now()
        row = {"id":uuid.uuid4().hex,"owner":ref,**ticket.model_dump(),"qty":qty,"price":price,
               "estimated_value":float(qty)*price,"estimated_fee":float(qty)*price*FEE_RATE,
               "created_at":now,"expires_at":now+30,"status":"review","broker_status":None,
               "filled_qty":"0","filled_avg_price":None,"message":"Orderni tasdiqlash kutilmoqda."}
        self.save(row)
        return self.public(row)

    @staticmethod
    def public(row):
        return {k:v for k,v in row.items() if k!="owner"}

    def apply(self, row, order):
        if order.get("client_order_id")!="manual-"+row["id"] or order.get("side")!=row["side"] or normalized(order.get("symbol",""))!=normalized(row["symbol"]):
            raise BotError("Order identifikatori qo‘lda savdo jurnaliga mos emas. Alpaca’da tekshiring.")
        filled = float(order.get("filled_qty") or 0)
        price = float(order.get("filled_avg_price") or 0)
        if not math.isfinite(filled) or not math.isfinite(price) or filled<0 or filled>float(row["qty"])*(1+1e-9) or (filled>0 and price<=0):
            raise BotError("Order fill ma’lumoti noto‘g‘ri. Alpaca’da tekshiring.")
        status = order.get("status", "unknown")
        row.update(broker_status=status,filled_qty=str(filled),filled_avg_price=price or None,
                   status=status if status in TERMINAL else "submitted",message=f"Alpaca order holati: {status}.")
        self.save(row)

    async def submit(self, order_id, action):
        broker, ref = self.broker()
        row = next((r for r in self.rows() if r["id"]==order_id and r["owner"]==ref), None)
        if not row:
            raise BotError("Order ko‘rib chiqish yozuvi topilmadi yoki akkaunt o‘zgargan.")
        if row["status"]!="review":
            return self.public(row)  # Exact same intent can never cause a second POST.
        if not action.acknowledge_risk or (row["mode"]=="live" and action.confirmation!=f"LIVE {row['side'].upper()} {row['symbol']}"):
            raise BotError("Riskni va Live tasdiqlash matnini tekshiring.")
        if self.rt.market.now()>row["expires_at"]:
            raise BotError("30 soniyalik ko‘rib chiqish muddati tugadi. Narxni qayta hisoblang.")
        _, qty, price = await self.validate(ManualTicket(**{k:row[k] for k in ("symbol","side","amount","mode")}),row["qty"],row["id"])
        if abs(price/row["price"]-1)>.01:
            raise BotError("Narx 1% dan ko‘p o‘zgardi. Orderni qayta ko‘rib chiqing.")
        row.update(status="submitted",message="Order yuborildi; holati tekshirilmoqda.")
        self.save(row)
        try:
            order = await broker.submit_order(row["symbol"],row["side"],qty,"manual-"+row["id"])
        except BrokerError as exc:
            definite = not exc.uncertain and exc.status and 400<=exc.status<500 and exc.status!=429
            row.update(status="rejected" if definite else "uncertain",message="Order rad etildi." if definite else "Javob noaniq. Client ID orqali tekshiriladi; qayta yuborilmaydi.")
            self.save(row)
        else:
            self.apply(row,order)
        return self.public(row)

    async def snapshot(self):
        if not self.rt.broker:
            return {"mode":"demo","positions":[],"open_orders":[],"orders":[],"cash":None}
        broker, ref = self.broker()
        for row in [r for r in self.pending() if r["owner"]==ref]:
            try:
                self.apply(row, await broker.order_by_client_id("manual-"+row["id"]))
            except BrokerError as exc:
                if exc.status!=404:
                    raise
                # Not found does not prove the original POST was never accepted.
        account, positions, orders = await asyncio.gather(broker.account(),broker.positions(),broker.open_orders())
        if account_ref(account,self.rt.mode)!=ref:
            raise BotError("Akkaunt o‘zgargan. Qayta ulang.")
        return {"mode":self.rt.mode,"cash":account["cash"],"positions":[{k:p.get(k) for k in ("symbol","qty","qty_available","avg_entry_price","current_price","market_value","unrealized_pl")} for p in positions],
                "open_orders":[{k:o.get(k) for k in ("symbol","side","qty","filled_qty","status","type")} for o in orders],
                "orders":[self.public(r) for r in sorted(self.rows(),key=lambda r:r["created_at"],reverse=True) if r["owner"]==ref and r["status"]!="review"][:100]}
