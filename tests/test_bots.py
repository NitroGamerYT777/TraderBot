import asyncio
import copy
import math
import time
from datetime import datetime, timezone

import pytest
import httpx
from fastapi.testclient import TestClient

from app import main, bots, strategies
from app.alpaca import BrokerError
from app.bots import BotAction, BotConfig, BotEngine, BotError, account_ref, floor_qty, review_token
from app.market import Market


def historical(count=350, timeframe=900):
    end = int(time.time()//timeframe)*timeframe-timeframe
    result=[]
    for i in range(count):
        close=100+math.sin(i/7)*2
        result.append({"t":datetime.fromtimestamp(end-(count-1-i)*timeframe,timezone.utc).isoformat(),
                       "o":close,"h":close+1,"l":close-1,"c":close,"v":100})
    return result


class FakeBroker:
    def __init__(self, mode="paper"):
        self.mode=mode
        self.sent=[]
        self.canceled=[]
        self.position_rows=[]
        self.order_rows=[]
        self.order_results={}
        self.submit_error=None
        self.submit_status="filled"
        self.account_data={"id":f"{mode}-account", "status":"ACTIVE", "currency":"USD","cash":1000.,"equity":1000.,"trading_blocked":False,"account_blocked":False}

    async def account(self): return dict(self.account_data)
    async def positions(self): return copy.deepcopy(self.position_rows)
    async def open_orders(self): return copy.deepcopy(self.order_rows)
    async def bars(self, symbol, timeframe, limit=350): return historical(limit,strategies.MINUTES[timeframe]*60)
    async def cancel_order(self, order_id): self.canceled.append(order_id)
    async def close(self): pass

    async def submit_order(self,symbol,side,qty,client_order_id):
        self.sent.append({"symbol":symbol,"side":side,"qty":qty,"client_order_id":client_order_id})
        if self.submit_error: raise self.submit_error
        order={"id":"broker-order-1","symbol":symbol,"side":side,"client_order_id":client_order_id,
               "filled_qty":qty if self.submit_status=="filled" else "0","filled_avg_price":"100","status":self.submit_status}
        self.order_results[client_order_id]=order
        return order

    async def order_by_client_id(self,client_order_id):
        if client_order_id not in self.order_results: raise BrokerError("Not found",status=404)
        return copy.deepcopy(self.order_results[client_order_id])


@pytest.fixture
def runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"DATA",tmp_path)
    rt=main.Runtime()
    rt.mode="paper"
    rt.broker=FakeBroker()
    rt.account=dict(rt.broker.account_data)
    rt.market=Market(demo=False)
    rt.market.assets=[{"symbol":"BTC/USD","name":"Bitcoin","tradable":True,"min_order_size":"0.001","min_trade_increment":"0.001"}]
    rt.market.quotes={"BTC/USD":{"price":100.,"bid":99.95,"ask":100.05,"baseline":100.,"timestamp":time.time()}}
    return rt


def create(rt, **kwargs):
    return rt.bots.create(BotConfig(name="Bitcoin Guard",symbol="BTC/USD",**kwargs))


def action(bot,phrase=""):
    return BotAction(acknowledge_risk=True,confirmation=phrase,review_token=review_token(bot))


def test_crud_persistence_and_safe_restart(runtime):
    bot=create(runtime)
    assert runtime.broker.sent==[]
    asyncio.run(runtime.bots.start(bot["id"],action(bot)))
    assert bot["status"]=="running" and runtime.broker.sent==[]
    restarted=BotEngine(runtime,runtime.bots.path)
    restored=restarted.get(bot["id"])
    assert restored["status"]=="paused" and restored["requires_resume"]
    with pytest.raises(BotError): runtime.bots.delete(bot["id"])
    runtime.bots.pause(bot["id"])
    runtime.bots.update(bot["id"],BotConfig(**{**bot["config"],"name":"Renamed","max_position_usd":20}))
    assert bot["config"]["name"]=="Renamed"
    runtime.bots.delete(bot["id"])
    assert runtime.bots.public()==[]


def test_config_validation_and_sizing():
    with pytest.raises(ValueError): BotConfig(name="x",symbol="BTC/USD")
    with pytest.raises(ValueError): BotConfig(name="Bot",symbol="BTC/EUR")
    with pytest.raises(ValueError): BotConfig(name="Bot",symbol="BTC/USD",budget_usd=30,max_position_usd=50)
    with pytest.raises(ValueError): BotConfig(name="Bot",symbol="BTC/USD",take_profit_pct=1,stop_loss_pct=2)
    with pytest.raises(ValueError): BotConfig(name="Bot",symbol="BTC/USD",budget_usd=float("nan"))
    with pytest.raises(ValueError): BotConfig(name="Bot",symbol="BTC/USD",budget_usd=20,risk_pct=.1)
    c=BotConfig(name="Bot",symbol="BTC/USD").model_dump()
    assert 0<strategies.position_size(c,100)<=c["max_position_usd"]
    assert strategies.position_size(c,1)<1
    assert floor_qty("0.123456789","0.0001")=="0.1234"
    with pytest.raises(BotError): floor_qty(1,0)


def test_live_mode_and_review_gate(runtime):
    bot=create(runtime,mode="live")
    with pytest.raises(BotError): asyncio.run(runtime.bots.start(bot["id"],action(bot)))
    runtime.mode="live";runtime.broker=FakeBroker("live");runtime.account=dict(runtime.broker.account_data)
    with pytest.raises(BotError): asyncio.run(runtime.bots.start(bot["id"],BotAction()))
    with pytest.raises(BotError): asyncio.run(runtime.bots.start(bot["id"],action(bot,"LIVE Wrong Bot")))
    approved=action(bot,"LIVE Bitcoin Guard")
    bot["config"]["budget_usd"]=200
    with pytest.raises(BotError,match="o‘zgargan"): asyncio.run(runtime.bots.start(bot["id"],approved))
    asyncio.run(runtime.bots.start(bot["id"],action(bot,"LIVE Bitcoin Guard")))
    assert bot["status"]=="running" and not runtime.broker.sent


def test_unmanaged_positions_account_and_reservation_guard(runtime):
    bot=create(runtime)
    runtime.broker.position_rows=[{"symbol":"BTCUSD","qty":"1","qty_available":"1"}]
    with pytest.raises(BotError,match="pozitsiya"): asyncio.run(runtime.bots.start(bot["id"],action(bot)))
    runtime.broker.position_rows=[]
    runtime.broker.account_data["cash"]=50
    with pytest.raises(BotError,match="yetarli emas"): asyncio.run(runtime.bots.start(bot["id"],action(bot)))
    runtime.broker.account_data["cash"]=1000
    asyncio.run(runtime.bots.start(bot["id"],action(bot)))
    other=runtime.bots.create(BotConfig(name="Duplicate coin",symbol="BTC/USD"))
    with pytest.raises(BotError,match="boshqa bot"): asyncio.run(runtime.bots.start(other["id"],action(other)))
    with pytest.raises(BotError): runtime.bots.check_switch({"id":"other-account"},"paper")
    runtime.bots.check_switch(runtime.account,"paper")


def test_new_signal_places_one_sized_order(runtime,monkeypatch):
    bot=create(runtime)
    async def run():
        await runtime.bots.start(bot["id"],action(bot))
        await runtime.bots.tick(bot)
        assert runtime.broker.sent==[]  # Starting never replays a historical signal.
        bot["last_bar"]=0
        monkeypatch.setattr(bots,"signal",lambda bars:{"action":"buy","reason":"Test signal"})
        await runtime.bots.tick(bot)
        await runtime.bots.tick(bot)
    asyncio.run(run())
    assert len(runtime.broker.sent)==1 and bot["qty"]>0 and bot["day_entries"]==1
    assert float(runtime.broker.sent[0]["qty"])*100.05<=25
    assert bot["fee_estimate"]>0


def test_stale_and_wide_spread_block_orders(runtime,monkeypatch):
    bot=create(runtime)
    async def run():
        await runtime.bots.start(bot["id"],action(bot))
        bot["last_bar"]=0
        monkeypatch.setattr(bots,"signal",lambda bars:{"action":"buy","reason":"Test"})
        runtime.market.quotes["BTC/USD"]["timestamp"]=time.time()-30
        with pytest.raises(BotError): await runtime.bots.tick(bot)
        runtime.market.quotes["BTC/USD"].update(timestamp=time.time(),bid=95,ask=105)
        await runtime.bots.tick(bot)
    asyncio.run(run())
    assert not runtime.broker.sent
    assert "Spread" in bot["message"]


def test_uncertain_order_survives_restart_without_resubmit(runtime):
    bot=create(runtime)
    bot["owner"]=account_ref(runtime.account,"paper")
    runtime.broker.submit_error=BrokerError("timeout",uncertain=True)
    asyncio.run(runtime.bots.submit(bot,"buy","0.100","Test"))
    assert bot["pending"] and len(runtime.broker.sent)==1
    cid=bot["pending"]["client_order_id"]
    asyncio.run(runtime.bots.reconcile(bot))
    assert bot["pending"] and len(runtime.broker.sent)==1
    runtime.broker.order_results[cid]={"id":"found","symbol":"BTC/USD","side":"buy","client_order_id":cid,"filled_qty":"0.100","filled_avg_price":"100","status":"filled"}
    restored=BotEngine(runtime,runtime.bots.path)
    recovered=restored.get(bot["id"])
    asyncio.run(restored.reconcile(recovered))
    assert recovered["pending"] is None and recovered["qty"]==pytest.approx(.1)
    assert recovered["requires_resume"] and len(runtime.broker.sent)==1
    assert recovered["cost"]==pytest.approx(10.025)


def test_partial_fills_are_applied_once_and_cancel_remainder(runtime):
    bot=create(runtime)
    runtime.broker.submit_status="new"
    asyncio.run(runtime.bots.submit(bot,"buy","0.100","Test"))
    cid=bot["pending"]["client_order_id"]
    order={"id":"partial","symbol":"BTCUSD","side":"buy","client_order_id":cid,"filled_qty":"0.040","filled_avg_price":"100","status":"partially_filled"}
    runtime.bots.apply_order(bot,order);runtime.bots.apply_order(bot,order)
    assert bot["qty"]==pytest.approx(.04) and bot["cost"]==pytest.approx(4.01)
    bot["pending"]["created_at"]=time.time()-60
    runtime.broker.order_results[cid]=order
    asyncio.run(runtime.bots.reconcile(bot))
    assert runtime.broker.canceled==["partial"]
    assert bot["pending"] is not None
    order.update(filled_qty="0.100",filled_avg_price="101",status="filled")
    runtime.bots.apply_order(bot,order)
    assert bot["pending"] is None and bot["qty"]==pytest.approx(.1)
    assert bot["cost"]==pytest.approx(10.1*1.0025)


def test_pause_preserves_stop_and_never_sells_unowned_qty(runtime):
    bot=create(runtime)
    bot.update(status="paused",owner=account_ref(runtime.account,"paper"),qty=.1,cost=10.025)
    runtime.market.quotes["BTC/USD"].update(bid=97,ask=97.1,price=97.05)
    runtime.broker.position_rows=[{"symbol":"BTCUSD","qty":"1.1","qty_available":"1.1"}]
    asyncio.run(runtime.bots.tick(bot))
    assert len(runtime.broker.sent)==1
    assert runtime.broker.sent[0]["side"]=="sell" and float(runtime.broker.sent[0]["qty"])==pytest.approx(.1)
    assert bot["qty"]==0 and bot["status"]=="paused"


def test_daily_loss_and_trade_limit_are_enforced(runtime,monkeypatch):
    bot=create(runtime)
    async def run():
        await runtime.bots.start(bot["id"],action(bot))
        bot.update(day=int(time.time()//86400),day_start=100,realized=-4)
        await runtime.bots.tick(bot)
        assert bot["halted_day"]==bot["day"]
        bot.update(day_start=100,realized=0,halted_day=None,day_entries=4,last_bar=0)
        monkeypatch.setattr(bots,"signal",lambda bars:{"action":"buy","reason":"Test"})
        await runtime.bots.tick(bot)
    asyncio.run(run())
    assert not runtime.broker.sent and "limit" in bot["message"]


def test_definite_rejection_clears_pending_and_unknown_response_does_not(runtime):
    bot=create(runtime)
    runtime.broker.submit_error=BrokerError("bad request",status=422)
    asyncio.run(runtime.bots.submit(bot,"buy","0.1","test"))
    assert bot["pending"] is None and bot["status"]=="error"
    runtime.broker.submit_error=BrokerError("server error",status=500,uncertain=True)
    asyncio.run(runtime.bots.submit(bot,"buy","0.1","test"))
    assert bot["pending"] is not None


def test_storage_failure_prevents_order_submission(runtime,monkeypatch):
    bot=create(runtime)
    def fail(*args,**kwargs): raise OSError("disk full")
    monkeypatch.setattr(runtime.bots,"db",fail)
    with pytest.raises(BotError): asyncio.run(runtime.bots.submit(bot,"buy","0.1","test"))
    assert runtime.broker.sent==[] and runtime.bots.storage_error


def test_one_process_owns_bot_directory(runtime):
    runtime.bots.acquire()
    second=BotEngine(runtime,runtime.bots.path)
    second.acquire()
    try:
        assert second.storage_error
        with pytest.raises(BotError): second.create(BotConfig(name="Another Bot",symbol="BTC/USD"))
    finally:
        second.release();runtime.bots.release()


def test_indicators_and_no_lookahead_backtest(monkeypatch):
    assert strategies.rsi([1.]*50)==50
    assert strategies.rsi(list(range(1,51)))==100
    assert strategies.rsi(list(range(50,0,-1)))==0
    assert strategies.ema([5.]*80,20)[-1]==5
    bars=[{"time":i*900.,"o":100.,"h":100.,"l":100.,"c":100.} for i in range(110)]
    bars[100].update(h=110,l=90)
    seen=[]
    def rule(history):
        seen.append(history[-1]["time"])
        return {"action":"buy" if len(seen)==1 else "wait"}
    monkeypatch.setattr(strategies,"signal",rule)
    result=strategies.backtest(bars,BotConfig(name="Bot",symbol="BTC/USD").model_dump())
    assert seen[0]==bars[99]["time"]  # Signal never sees its fill bar.
    assert result["trades"]==1 and result["recent_trades"][0]["reason"]=="Stop-loss"
    assert result["equity"]<100 and result["fee_pct"]>0 and result["slippage_pct"]>0
    # Unclosed and malformed bars are excluded.
    raw=historical(2);raw.append({"t":datetime.now(timezone.utc).isoformat(),"o":100,"h":101,"l":99,"c":100})
    raw.append({**raw[0],"c":float("nan")})
    assert len(strategies.closed_bars(raw,"15Min",time.time()))==2
    # Closed candles remain current until their successor should have closed.
    assert strategies.bars_fresh([{"time":0}],"15Min",1750)
    assert not strategies.bars_fresh([{"time":0}],"15Min",1900)


def test_api_create_defaults_validation_and_no_demo_trading(tmp_path,monkeypatch):
    monkeypatch.setattr(main,"DATA",tmp_path)
    rt=main.Runtime();monkeypatch.setattr(main,"runtime",rt)
    async def idle(self): await asyncio.Event().wait()
    monkeypatch.setattr(BotEngine,"run",idle)
    with TestClient(main.app,base_url="http://127.0.0.1:8000") as c:
        c.headers["X-Auto-Trader-Token"]=c.get("/api/bootstrap").json()["token"]
        r=c.post("/api/bots",json={"name":"Demo Draft","symbol":"BTC/USD"})
        assert r.status_code==201
        bot_id=r.json()["id"]
        bot=c.get("/api/bots").json()["bots"][0]
        assert bot["status"]=="draft" and bot["config"]["mode"]=="paper"
        assert "owner" not in bot
        assert c.post(f"/api/bots/{bot_id}/start",json={"acknowledge_risk":True,"review_token":bot["review_token"]}).status_code==400
        assert c.post("/api/bots",json={"name":"Invalid","symbol":"XAU/USD","budget_usd":-1}).status_code==422
        assert c.post("/api/bots",json={"name":"Demo Draft","symbol":"ETH/USD"}).status_code==400
        assert c.get(f"/api/bots/{bot_id}/events").json()["events"]
        assert c.post(f"/api/bots/{bot_id}/backtest").status_code==400
        assert c.delete(f"/api/bots/{bot_id}").status_code==200
        assert not c.get("/api/bots").json()["bots"]


def test_broker_order_contract_no_post_retry_and_bars_pagination():
    from app.alpaca import Alpaca
    requests=[]
    def transport(request):
        requests.append(request)
        if request.method=="POST":
            raise httpx.ReadTimeout("timeout",request=request)
        if request.url.path.endswith("/bars"):
            assert request.url.host=="data.alpaca.markets"
            assert request.url.params["sort"]=="desc"
            if "page_token" not in request.url.params:
                return httpx.Response(200,json={"bars":{"BTC/USD":[{"t":"2026-10-20T12:15:00Z"}]},"next_page_token":"page2"})
            assert request.url.params["page_token"]=="page2"
            return httpx.Response(200,json={"bars":{"BTC/USD":[{"t":"2026-10-20T12:00:00Z"}]}})
        raise AssertionError(request.url)
    async def run():
        broker=Alpaca("mock-key","mock-secret","paper")
        await broker.client.aclose()
        broker.client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        try:
            with pytest.raises(BrokerError) as error:
                await broker.submit_order("BTC/USD","buy","0.001","unique-test-id")
            assert error.value.uncertain
            assert len(requests)==1 and requests[0].url.host=="paper-api.alpaca.markets"
            import json
            payload=json.loads(requests[0].content)
            assert payload=={"symbol":"BTC/USD","side":"buy","qty":"0.001","type":"market","time_in_force":"gtc","client_order_id":"unique-test-id"}
            bars=await broker.bars("BTC/USD","15Min",2)
            assert len(bars)==2 and bars[0]["t"]<bars[1]["t"]
        finally:
            await broker.close()
    asyncio.run(run())
