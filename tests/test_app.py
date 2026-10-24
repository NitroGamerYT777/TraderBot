import asyncio
import math
import time

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import main
from app.alpaca import Alpaca
from app.market import Market, correlation


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(main,"DATA",tmp_path)
    monkeypatch.setattr(main,"runtime",main.Runtime())
    vault = {}
    monkeypatch.setattr(main.keyring,"set_password",lambda service,name,value:vault.update({name:value}))
    monkeypatch.setattr(main.keyring,"get_password",lambda service,name:vault.get(name))
    monkeypatch.setattr(main.keyring,"delete_password",lambda service,name:vault.pop(name,None))
    with TestClient(main.app,base_url="http://127.0.0.1:8000") as c:
        c.headers["X-Auto-Trader-Token"] = c.get("/api/bootstrap").json()["token"]
        yield c, vault


def test_bootstrap_demo_and_history(client):
    c,_=client
    d=c.get("/api/bootstrap").json()
    assert d["mode"] == "demo" and len(d["assets"])>20
    assert not any(a["tradable"] for a in d["assets"])
    assert d["analytics"]["samples"] >= 30
    p=c.get("/api/portfolio?period=1W").json()
    assert p["demo"] and len(p["points"])==200 and p["points"][-1]["equity"]==10000
    assert c.get("/api/portfolio?period=invalid").status_code==422
    assert c.get("/").status_code==200


def test_settings_validation_and_origin(client):
    c,_=client
    payload={"refresh_hz":10,"symbols":["BTC/USD","ETH/USD"]}
    assert c.put("/api/settings",json=payload).status_code==200
    assert c.get("/api/bootstrap").json()["settings"]["refresh_hz"]==10
    assert c.put("/api/settings",json={**payload,"refresh_hz":11}).status_code==422
    assert c.put("/api/settings",json={**payload,"symbols":["XAU/USD"]}).status_code==422
    assert c.put("/api/settings",json=payload,headers={"Origin":"https://evil.example"}).status_code==403
    assert c.put("/api/settings",json=payload,headers={"X-Auto-Trader-Token":"bad"}).status_code==403
    assert c.get("/api/bootstrap",headers={"Host":"evil.example"}).status_code==400


def fake_transport(request):
    if request.headers.get("APCA-API-KEY-ID")!="test-key":
        return httpx.Response(401,json={"message":"remote-secret-echo"})
    if request.url.path=="/v2/account":
        return httpx.Response(200,json={"equity":"1250","cash":"250","buying_power":"500","last_equity":"1200","status":"ACTIVE","currency":"USD"})
    if request.url.path=="/v2/assets":
        return httpx.Response(200,json=[{"symbol":"BTC/USD","name":"Bitcoin","tradable":True},{"symbol":"BAD/USD","tradable":False}])
    if request.url.path.endswith("/history"):
        assert request.url.params["intraday_reporting"]=="continuous"
        assert request.url.params["pnl_reset"]=="no_reset"
        return httpx.Response(200,json={"timestamp":[1,2,3],"equity":[1200,None,1250]})
    raise AssertionError(request.url)


@pytest.fixture
def mock_broker(monkeypatch):
    original=Alpaca.__init__
    def init(self,*args,**kwargs):
        original(self,*args,**kwargs)
        # No sockets are opened by an AsyncClient until its first request.
        self.client=httpx.AsyncClient(transport=httpx.MockTransport(fake_transport),headers={"APCA-API-KEY-ID":self.key,"APCA-API-SECRET-KEY":self.secret})
    async def stream(self,market,get_symbols=None):
        market.status="connected"
        await asyncio.Event().wait()
    monkeypatch.setattr(Alpaca,"__init__",init)
    monkeypatch.setattr(Alpaca,"stream",stream)


def test_real_connection_no_secret_leakage_and_disconnect(client,mock_broker):
    c,vault=client
    assert c.post("/api/connection",json={"api_key":"test-key","secret_key":"test-secret","mode":"live","remember":True}).status_code==200
    assert "credentials" in vault
    d=c.get("/api/bootstrap")
    assert "test-key" not in d.text and "test-secret" not in d.text
    assert d.json()["mode"]=="live" and d.json()["account"]["equity"]==1250
    assert len(d.json()["assets"])==1
    assert "test-secret" not in (main.DATA/"settings.json").read_text()
    p=c.get("/api/portfolio?period=1D").json()
    assert not p["demo"] and p["points"]==[{"time":1,"equity":1200},{"time":3,"equity":1250}]
    # Failed replacement must preserve the existing connected account.
    bad=c.post("/api/connection",json={"api_key":"bad","secret_key":"secret","mode":"paper"})
    assert bad.status_code==400 and "remote-secret-echo" not in bad.text
    assert c.get("/api/bootstrap").json()["mode"]=="live"
    assert c.delete("/api/connection").status_code==200
    assert not vault and c.get("/api/bootstrap").json()["mode"]=="demo"
    assert c.post("/v2/orders",json={}).status_code==404


def test_validation_never_echoes_secret(client):
    c,_=client
    response=c.post("/api/connection",json={"api_key":"private-key","secret_key":"private-secret","mode":"not-a-mode"})
    assert response.status_code==422
    assert "private-key" not in response.text and "private-secret" not in response.text


def test_websocket_snapshot(client):
    c,_=client
    token=c.get("/api/bootstrap").json()["token"]
    with c.websocket_connect("ws://127.0.0.1:8000/ws",headers={"Origin":"http://127.0.0.1:8000"}) as ws:
        ws.send_json({"token":token})
        assert ws.receive_json()["mode"]=="demo"
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("ws://127.0.0.1:8000/ws",headers={"Origin":"https://evil.example"}):
            pass
    with c.websocket_connect("ws://127.0.0.1:8000/ws",headers={"Origin":"http://127.0.0.1:8000"}) as ws:
        ws.send_json({"token":"bad"})
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()


def test_keyring_failure_does_not_replace_account(client,mock_broker,monkeypatch):
    c,_=client
    def fail(*args):
        raise RuntimeError("sensitive backend message")
    monkeypatch.setattr(main.keyring,"set_password",fail)
    r=c.post("/api/connection",json={"api_key":"test-key","secret_key":"test-secret","remember":True})
    assert r.status_code==400 and "sensitive" not in r.text
    assert c.get("/api/bootstrap").json()["mode"]=="demo"


def test_session_connection_without_os_vault(client,mock_broker,monkeypatch):
    c,_=client
    def no_backend(*args):
        raise main.keyring.errors.NoKeyringError()
    monkeypatch.setattr(main.keyring,"delete_password",no_backend)
    r=c.post("/api/connection",json={"api_key":"test-key","secret_key":"test-secret","remember":False})
    assert r.status_code==200
    assert not c.get("/api/bootstrap").json()["settings"]["remember"]


def test_stream_auth_subscription_and_quote(monkeypatch):
    import json
    from app import alpaca
    sent=[]
    class Stream:
        async def __aenter__(self):
            return self
        async def __aexit__(self,*args):
            pass
        async def send(self,raw):
            sent.append(json.loads(raw))
        def __init__(self):
            self.iterator = self.messages()
        async def recv(self):
            return await anext(self.iterator)
        async def messages(self):
            yield json.dumps([{"T":"success","msg":"authenticated"}])
            yield json.dumps([{"T":"subscription","quotes":["BTC/USD"]}])
            yield json.dumps([{"T":"q","S":"BTC/USD","bp":100,"ap":102,"t":"2026-10-23T12:00:00Z"}])
            raise asyncio.CancelledError()
    monkeypatch.setattr(alpaca.websockets,"connect",lambda *args,**kwargs:Stream())
    async def run():
        broker=Alpaca("test-key","test-secret","paper")
        market=Market(demo=False)
        market.assets=[{"symbol":"BTC/USD"}]
        try:
            with pytest.raises(asyncio.CancelledError):
                await broker.stream(market)
        finally:
            await broker.close()
        assert market.status=="connected" and market.quotes["BTC/USD"]["price"]==101
    asyncio.run(run())
    assert sent==[{"action":"auth","key":"test-key","secret":"test-secret"},{"action":"subscribe","quotes":["BTC/USD"]}]


def test_correlations_and_stale_intervals():
    a=list(range(40))
    assert math.isclose(correlation(a,a),1)
    assert math.isclose(correlation(a,[-x for x in a]),-1)
    assert correlation([1]*40,[1]*40) is None
    assert correlation(a[:10],a[:10]) is None
    m=Market(demo=False)
    now=time.time()
    m.update_quote({"S":"BTC/USD","bp":99,"ap":101,"t":"2026-10-23T12:00:00Z"})
    m.sample(now+100000)
    assert m.analytics(["BTC/USD"])["samples"]==0
    # Out-of-order and invalid quotes cannot replace good data.
    m.update_quote({"S":"BTC/USD","bp":49,"ap":51,"t":"2026-10-22T12:00:00Z"})
    m.update_quote({"S":"BTC/USD","bp":200,"ap":100,"t":"2026-10-24T12:00:00Z"})
    assert m.quotes["BTC/USD"]["price"]==100
