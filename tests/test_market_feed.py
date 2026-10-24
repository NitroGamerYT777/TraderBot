"""Regression tests for the 73-symbol subscription failure and provider clock skew."""
import asyncio
import json
import time
from datetime import datetime, timezone
from email.utils import format_datetime
from types import SimpleNamespace

import httpx
import pytest

from app import alpaca
from app.alpaca import Alpaca, BrokerError
from app.bots import BotEngine, BotError
from app.market import Market


def utc(value=None):
    return datetime.fromtimestamp(value if value is not None else time.time(),timezone.utc).isoformat()


class Socket:
    def __init__(self,sent,limit=20,on_quote=None):
        self.sent,self.limit,self.on_quote = sent,limit,on_quote
        self.messages = [[{"T":"success","msg":"authenticated"}]]
        self.subscribed = set()

    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass

    async def send(self,raw):
        message = json.loads(raw)
        self.sent.append(message)
        if message["action"]=="auth": return
        wanted = set(message["quotes"])
        if message["action"]=="subscribe" and len(self.subscribed|wanted)>self.limit:
            self.messages.append([{"T":"error","code":405,"msg":"sensitive remote message ignored"}])
            return
        if message["action"]=="unsubscribe": self.subscribed -= wanted
        else: self.subscribed |= wanted
        self.messages.append([{"T":"subscription","quotes":sorted(self.subscribed)}])
        if message["action"]=="subscribe":
            self.messages.append([{"T":"q","S":sorted(wanted)[0],"bp":100,"ap":101,"t":utc()}])

    async def recv(self):
        if not self.messages: raise asyncio.CancelledError()
        messages = self.messages.pop(0)
        if messages[0]["T"]=="q" and self.on_quote:
            callback,self.on_quote = self.on_quote,None
            callback()
        return json.dumps(messages)


def test_73_assets_streams_only_focus_symbols(monkeypatch):
    sent=[]
    monkeypatch.setattr(alpaca.websockets,"connect",lambda *a,**k:Socket(sent))
    async def run():
        broker=Alpaca("test","test","paper")
        market=Market(demo=False)
        market.assets=[{"symbol":s} for s in ["BTC/USD","ETH/USD"]+[f"COIN{i}/USD" for i in range(71)]]
        try:
            with pytest.raises(asyncio.CancelledError):
                await broker.stream(market,lambda:["BTC/USD","ETH/USD"])
        finally: await broker.close()
        assert market.status=="connected" and market.stream_error is None
    asyncio.run(run())
    subscriptions=[m["quotes"] for m in sent if m["action"]=="subscribe"]
    assert subscriptions==[["BTC/USD","ETH/USD"]]


def test_subscription_changes_in_same_socket(monkeypatch):
    sent=[];focus=["BTC/USD","ETH/USD"]
    def change(): focus[:]=["ETH/USD","SOL/USD"]
    monkeypatch.setattr(alpaca.websockets,"connect",lambda *a,**k:Socket(sent,on_quote=change))
    async def run():
        broker=Alpaca("test","test","paper");market=Market(demo=False)
        market.assets=[{"symbol":s} for s in ["BTC/USD","ETH/USD","SOL/USD"]]
        try:
            with pytest.raises(asyncio.CancelledError): await broker.stream(market,lambda:focus)
        finally: await broker.close()
        assert market.subscribed_symbols==["ETH/USD","SOL/USD"]
    asyncio.run(run())
    assert {"action":"unsubscribe","quotes":["BTC/USD"]} in sent
    assert {"action":"subscribe","quotes":["SOL/USD"]} in sent
    assert sum(m["action"]=="auth" for m in sent)==1


def test_405_reduces_subscription_and_recovers(monkeypatch):
    sent=[]
    monkeypatch.setattr(alpaca.websockets,"connect",lambda *a,**k:Socket(sent,limit=1))
    async def run():
        broker=Alpaca("test","test","paper");market=Market(demo=False)
        market.assets=[{"symbol":s} for s in ["BTC/USD","ETH/USD","SOL/USD"]]
        try:
            with pytest.raises(asyncio.CancelledError): await broker.stream(market,lambda:["BTC/USD","ETH/USD","SOL/USD"])
        finally: await broker.close()
        assert market.status=="connected" and market.stream_error is None
        assert market.subscribed_symbols==["BTC/USD"]
    asyncio.run(run())
    assert [len(m["quotes"]) for m in sent if m["action"]=="subscribe"]==[3,1]


def test_rest_batches_all_73_assets_and_skips_bad_pair():
    async def run():
        broker=Alpaca("test","test","paper");market=Market(demo=False)
        symbols=["BTC/USD","BAD/USD"]+[f"COIN{i}/USD" for i in range(71)]
        market.assets=[{"symbol":s} for s in symbols]
        batches=[]
        async def latest(requested):
            batches.append(list(requested))
            if "BAD/USD" in requested: raise BrokerError("invalid",status=400)
            return {s:{"bp":100,"ap":101,"t":utc()} for s in requested}
        broker.latest_quotes=latest
        try:
            await broker.refresh_quotes_once(market)
            assert len(market.quotes)==72 and market.quotes["BTC/USD"]["source"]=="rest"
            assert all(len(b)<=25 for b in batches)
            batches.clear()
            await broker.refresh_quotes_once(market)
            assert not any("BAD/USD" in b for b in batches)
            assert market.health()["status"]=="rest"
        finally: await broker.close()
    asyncio.run(run())


def test_provider_clock_keeps_fresh_quotes_fresh_and_old_quotes_stale(tmp_path):
    provider = time.time()-8*3600
    def transport(request):
        assert request.url.host=="data.alpaca.markets" and request.method=="GET"
        return httpx.Response(200,headers={"Date":format_datetime(datetime.fromtimestamp(provider,timezone.utc),usegmt=True)},
            json={"quotes":{"BTC/USD":{"bp":100,"ap":101,"t":utc(provider-1)},"ETH/USD":{"bp":10,"ap":11,"t":utc(provider-60)}}})
    async def run():
        broker=Alpaca("test","test","paper");await broker.client.aclose()
        broker.client=httpx.AsyncClient(transport=httpx.MockTransport(transport))
        market=Market(demo=False);market.clock=broker.market_time
        market.assets=[{"symbol":"BTC/USD"},{"symbol":"ETH/USD"}]
        try:
            await broker.refresh_quotes_once(market)
            snapshot={a["symbol"]:a for a in market.snapshot()}
            assert not snapshot["BTC/USD"]["stale"] and snapshot["ETH/USD"]["stale"]
            assert snapshot["BTC/USD"]["timestamp"]==pytest.approx(provider-1)
            assert market.health()["status"]=="rest" and abs(market.health()["clock_skew_seconds"]-28800)<3
            assert market.health()["clock_warning"]
            first = broker.market_time()
            await broker.refresh_quotes_once(market)
            assert broker.market_time()>=first  # Rounded HTTP dates cannot rewind charts.
            market.sample()
            assert "BTC/USD" in market.series[-1]["prices"] and "ETH/USD" not in market.series[-1]["prices"]
            engine=BotEngine(SimpleNamespace(market=market),tmp_path/"bots.sqlite3")
            assert engine.quote({"config":{"symbol":"BTC/USD"}})
            with pytest.raises(BotError): engine.quote({"config":{"symbol":"ETH/USD"}})
        finally: await broker.close()
    asyncio.run(run())


def test_old_rest_quote_does_not_replace_new_websocket_quote():
    market=Market(demo=False)
    now=time.time()
    market.update_quote({"S":"BTC/USD","bp":100,"ap":102,"t":utc(now)})
    market.update_quote({"S":"BTC/USD","bp":50,"ap":52,"t":utc(now-2)},source="rest")
    market.update_quote({"S":"BTC/USD","bp":100,"ap":102,"t":utc(now)},source="rest")
    assert market.quotes["BTC/USD"]["price"]==101
    assert market.quotes["BTC/USD"]["source"]=="websocket"
