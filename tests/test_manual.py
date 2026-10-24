import asyncio
import copy
import time

import pytest
from fastapi.testclient import TestClient

from app import main
from app.alpaca import BrokerError
from app.bots import BotEngine, BotError, account_ref
from app.manual import ManualTrading, ManualTicket, ManualConfirm
from test_bots import runtime, create, action


def ticket(**kwargs):
    return ManualTicket(symbol="BTC/USD",side="buy",amount=25,mode="paper",**kwargs)


def review(rt, **kwargs):
    return asyncio.run(rt.manual.preview(ticket(**kwargs)))


def send(rt, row, confirmation=""):
    return asyncio.run(rt.manual.submit(row["id"],ManualConfirm(acknowledge_risk=True,confirmation=confirmation)))


def test_review_and_submit_exactly_once(runtime):
    r=review(runtime)
    assert not runtime.broker.sent and r["qty"]=="0.249"
    assert "owner" not in r and r["estimated_fee"]>0
    with pytest.raises(BotError):
        asyncio.run(runtime.manual.submit(r["id"],ManualConfirm()))
    result=send(runtime,r)
    assert result["status"]=="filled" and len(runtime.broker.sent)==1
    assert send(runtime,r)["status"]=="filled" and len(runtime.broker.sent)==1
    assert runtime.broker.sent[0]["client_order_id"]=="manual-"+r["id"]


def test_ambiguous_post_survives_restart_and_blocks_bot(runtime):
    r=review(runtime)
    runtime.broker.submit_error=BrokerError("timeout",uncertain=True)
    assert send(runtime,r)["status"]=="uncertain"
    runtime.manual=ManualTrading(runtime)
    assert send(runtime,r)["status"]=="uncertain" and len(runtime.broker.sent)==1
    asyncio.run(runtime.manual.snapshot())  # 404 is not proof it was never accepted
    assert runtime.manual.pending()
    bot=create(runtime)
    with pytest.raises(BotError,match="yakunlanmagan"):
        asyncio.run(runtime.bots.start(bot["id"],action(bot)))
    with pytest.raises(BotError):
        review(runtime)
    with pytest.raises(BotError):
        runtime.manual.check_switch({"id":"other"},"paper")
    cid="manual-"+r["id"]
    runtime.broker.order_results[cid]={"id":"broker","client_order_id":cid,"symbol":"BTCUSD","side":"buy","status":"filled","filled_qty":r["qty"],"filled_avg_price":"100"}
    snap=asyncio.run(runtime.manual.snapshot())
    assert snap["orders"][0]["status"]=="filled" and not runtime.manual.pending()
    assert len(runtime.broker.sent)==1


def test_live_and_account_review_binding(runtime):
    runtime.mode="live"
    runtime.account["id"]="live-account"
    runtime.broker.account_data["id"]="live-account"
    r=asyncio.run(runtime.manual.preview(ManualTicket(symbol="BTC/USD",side="buy",amount=25,mode="live")))
    with pytest.raises(BotError):send(runtime,r)
    assert not runtime.broker.sent
    assert send(runtime,r,"LIVE BUY BTC/USD")["status"]=="filled"
    runtime.account["id"]="different-account"
    with pytest.raises(BotError):send(runtime,r,"LIVE BUY BTC/USD")
    assert len(runtime.broker.sent)==1


def test_stale_expired_and_price_drift(runtime,monkeypatch):
    r=review(runtime)
    monkeypatch.setattr(runtime.market,"now",lambda:r["expires_at"]+1)
    with pytest.raises(BotError,match="muddati"):
        send(runtime,r)
    monkeypatch.setattr(runtime.market,"now",lambda:r["created_at"])
    runtime.market.quotes["BTC/USD"].update(bid=102,ask=102.1,price=102.05)
    with pytest.raises(BotError,match="1%"):
        send(runtime,r)
    runtime.market.quotes["BTC/USD"]["timestamp"]-=100
    with pytest.raises(BotError):review(runtime)
    assert not runtime.broker.sent


def test_bot_owned_and_reserved_cash(runtime):
    bot=create(runtime,budget_usd=990)
    bot.update(status="running",owner=account_ref(runtime.account,"paper"))
    with pytest.raises(BotError,match="bot tomonidan"):
        review(runtime)
    bot["status"]="paused"
    bot.update(qty=.1,cost=10)
    with pytest.raises(BotError,match="bot tomonidan"):
        review(runtime)
    bot.update(qty=0,cost=0,status="running")
    runtime.market.assets.append({"symbol":"ETH/USD","tradable":True,"min_order_size":".001","min_trade_increment":".001"})
    runtime.market.quotes["ETH/USD"]=copy.deepcopy(runtime.market.quotes["BTC/USD"])
    with pytest.raises(BotError,match="Erkin naqd"):
        asyncio.run(runtime.manual.preview(ManualTicket(symbol="ETH/USD",side="buy",amount=25,mode="paper")))
    assert not runtime.broker.sent


def test_sell_only_available_qty_and_recheck(runtime):
    t=ManualTicket(symbol="BTC/USD",side="sell",amount=.2,mode="paper")
    with pytest.raises(BotError,match="miqdori yetmaydi"):
        asyncio.run(runtime.manual.preview(t))
    runtime.broker.position_rows=[{"symbol":"BTCUSD","qty":".5","qty_available":".3"}]
    r=asyncio.run(runtime.manual.preview(t))
    runtime.broker.position_rows[0]["qty_available"]=".1"
    with pytest.raises(BotError):send(runtime,r)
    runtime.broker.position_rows[0]["qty_available"]=".3"
    assert send(runtime,r)["status"]=="filled"
    assert runtime.broker.sent[0]["side"]=="sell"


def test_external_order_and_account_blocks(runtime):
    r=review(runtime)
    runtime.broker.order_rows=[{"symbol":"BTCUSD"}]
    with pytest.raises(BotError,match="ochiq order"):
        send(runtime,r)
    runtime.broker.order_rows=[]
    runtime.broker.account_data["trading_blocked"]=True
    with pytest.raises(BotError):send(runtime,r)
    assert not runtime.broker.sent


def test_disk_failure_prevents_post(runtime,monkeypatch):
    r=review(runtime)
    def broken_save(row):raise OSError("disk full")
    monkeypatch.setattr(runtime.manual,"save",broken_save)
    with pytest.raises(OSError):send(runtime,r)
    assert not runtime.broker.sent


def test_manual_api_guards_and_no_secrets(runtime,monkeypatch):
    monkeypatch.setattr(main,"runtime",runtime)
    c=TestClient(main.app,base_url="http://127.0.0.1:8000")
    assert c.post('/api/manual/preview',json=ticket().model_dump()).status_code==403
    c.headers['X-Auto-Trader-Token']=main.TOKEN
    assert c.post('/api/manual/preview',json={**ticket().model_dump(),"amount":-1}).status_code==422
    r=c.post('/api/manual/preview',json=ticket().model_dump()).json()
    assert not runtime.broker.sent
    assert c.post('/api/manual/'+r['id']+'/submit',json={'acknowledge_risk':True}).json()['status']=='filled'
    assert len(runtime.broker.sent)==1
    assert c.post('/api/manual/'+r['id']+'/submit',json={'acknowledge_risk':True}).status_code==200
    snapshot=c.get('/api/manual').json()
    assert len(snapshot['orders'])==1 and 'owner' not in snapshot['orders'][0]


def test_rejection_does_not_resubmit(runtime):
    r=review(runtime)
    runtime.broker.submit_error=BrokerError("rejected",status=422)
    assert send(runtime,r)["status"]=="rejected"
    assert send(runtime,r)["status"]=="rejected" and len(runtime.broker.sent)==1
    assert not runtime.manual.pending()


def test_low_price_coin_sell_uses_units_not_usd_cap(runtime):
    runtime.market.assets.append({"symbol":"SHIB/USD","tradable":True,"min_order_size":"1","min_trade_increment":"1"})
    runtime.market.quotes["SHIB/USD"]={"price":.00002,"bid":.00001999,"ask":.00002001,"timestamp":time.time()}
    runtime.broker.position_rows=[{"symbol":"SHIBUSD","qty":"1000000","qty_available":"1000000"}]
    row=asyncio.run(runtime.manual.preview(ManualTicket(symbol="SHIB/USD",side="sell",amount=1000000,mode="paper")))
    assert row["qty"]=="1000000" and row["estimated_value"]>5
    with pytest.raises(ValueError):ManualTicket(symbol="BTC/USD",side="buy",amount=100001,mode="paper")
