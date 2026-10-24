"use strict";
const $ = id => document.getElementById(id);
const colors = ["#bbff6b", "#59ddd2", "#b5a1f0", "#edc280", "#6bce9d", "#ec96bf"];
const state = {data:null,token:null,page:"market",chart:"line",period:"1W",history:null,historyRequest:0,lastTable:0,socket:null,closing:false,selecting:false,rateDirty:false,transport:false};
const money = (n, currency="USD") => n == null || !Number.isFinite(Number(n)) ? "—" : new Intl.NumberFormat("en-US",{style:"currency",currency,minimumFractionDigits:2,maximumFractionDigits:Math.abs(n)<.01?8:Math.abs(n)<1?5:2}).format(n);
const pct = n => n == null ? "—" : `${n>=0?"+":""}${n.toFixed(2)}%`;
const short = s => s.split("/")[0];
function element(tag, cls, text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e;}
function toast(message,error=false){const t=$("toast");t.textContent=message;t.classList.remove("hidden");t.classList.toggle("error",error);clearTimeout(toast.timer);toast.timer=setTimeout(()=>t.classList.add("hidden"),5000);}
async function api(path, options={}){
  const response=await fetch(path,{...options,headers:{"Content-Type":"application/json","X-Auto-Trader-Token":state.token,...options.headers}});
  const data=await response.json();
  if(!response.ok)throw Error(typeof data.detail==="string"?data.detail:"So‘rov noto‘g‘ri. Maydonlarni tekshiring.");
  return data;
}
function setTrend(id,value){const e=$(id);e.textContent=pct(value);e.classList.toggle("positive",value!=null&&value>=0);e.classList.toggle("negative",value!=null&&value<0);}
function navigate(){
  const route=location.hash.slice(1);state.page=["market","balance","bots","account"].includes(route)?route:"market";
  document.querySelectorAll(".page").forEach(e=>e.classList.toggle("hidden",e.id!==`page-${state.page}`));
  document.querySelectorAll("[data-nav]").forEach(e=>{e.classList.toggle("active",e.dataset.nav===state.page);if(e.dataset.nav===state.page)e.setAttribute("aria-current","page");else e.removeAttribute("aria-current");});
  document.title=`Auto Trader · ${{market:"Bozor paneli",balance:"Mening balansim",bots:"Trader Bots",account:"Account"}[state.page]}`;
  if(state.page==="balance")loadHistory();
  if(state.data)render(state.data,true);
  requestAnimationFrame(drawAll);
  if(typeof matrixPageEnter==="function")matrixPageEnter();
  window.scrollTo({top:0,behavior:"instant"});
}
function render(data,force=false){
  const oldMode=state.data?.mode;state.data=data;
  const demo=data.mode==="demo", currency=data.account?.currency||"USD";
  const equity=demo?10000:data.account?.equity;
  $("header-balance").textContent=demo?"$10,000 · Demo":money(equity,currency);
  $("portfolio-value").textContent=money(equity,currency);
  $("portfolio-type").textContent=demo?"SIMULYATSIYA":`${data.mode.toUpperCase()} AKKAUNT`;
  $("balance-equity").textContent=money(equity,currency);
  $("balance-cash").textContent=money(demo?10000:data.account?.cash,currency);
  $("balance-power").textContent=money(demo?10000:data.account?.buying_power,currency);
  $("mode-label").textContent={demo:"DEMO MUHIT",paper:"PAPER AKKAUNT",live:"LIVE AKKAUNT"}[data.mode];
  $("stream-label").textContent=state.transport?data.message:"Server bilan aloqa uzildi. Qayta ulanmoqda…";
  $("stream-dot").style.background=state.transport?(demo?"#a0b276":data.status==="connected"?"#51a780":"#d5ae6f"):"#c66062";
  $("chart-source").textContent=demo?"Demo simulyatsiya":data.data_source==="rest"?"Alpaca · REST narxlari":"Alpaca · WebSocket narxlari";
  $("demo-banner").classList.toggle("hidden",!demo);
  $("hz-label").textContent=`${data.settings.refresh_hz} Hz`;
  $("clock").textContent=new Intl.DateTimeFormat("uz-UZ",{hour:"2-digit",minute:"2-digit",second:"2-digit",timeZoneName:"short"}).format(new Date());
  const streamWarning=!demo&&data.stream_error?`${data.stream_error.message}${data.data_source==="rest"?" Narxlar REST orqali olinmoqda.":""}`:null;
  const alert=[data.startup_error,data.account_error,data.bots_error,streamWarning,data.rest_error,data.clock_warning,
    !demo&&data.status==="waiting"?data.message:null,
    data.account_updated&&Date.now()/1000-data.account_updated>40?"Akkaunt balansi eskirgan. Oxirgi qiymat ko‘rsatilmoqda.":null,!state.transport?"Server bilan aloqa uzildi; ko‘rsatilgan ma’lumotlar eskirgan bo‘lishi mumkin.":null].filter(Boolean).join(" ");
  $("persistent-alert").textContent=alert;$("persistent-alert").classList.toggle("hidden",!alert);
  const btc=data.assets.find(a=>a.symbol==="BTC/USD"),eth=data.assets.find(a=>a.symbol==="ETH/USD");
  $("btc-price").textContent=money(btc?.price);$("eth-price").textContent=money(eth?.price);
  setTrend("btc-change",btc?.change);setTrend("eth-change",eth?.change);
  $("balance-mode").textContent=demo?"DEMO":data.mode.toUpperCase();
  $("balance-notice").textContent=demo?"DEMO · Balans raqamlari va tarix grafigi namunaviy. Haqiqiy balans uchun Account bo‘limida Alpaca’ni ulang.":`${data.mode.toUpperCase()} · Alpaca akkauntining portfel qiymati. Balans har 15 soniyada yangilanadi.`;
  $("account-badge").textContent=demo?"ULANMAGAN":`${data.mode.toUpperCase()} ULANGAN`;
  $("crypto-source-state").textContent=demo?"Demo":data.status==="connected"?"WebSocket":data.status==="rest"?"REST":"Kutilmoqda";
  $("disconnect-button").disabled=demo&&!data.settings.remember;
  if(!state.rateDirty){$("refresh-rate").value=data.settings.refresh_hz;$("rate-number").replaceChildren(document.createTextNode(data.settings.refresh_hz),element("span",null," Hz"));}
  if(oldMode!==data.mode){
    if(!demo)$("connection-mode").value=data.mode;
    $("remember-keys").checked=data.settings.remember;
    if(state.page==="balance")loadHistory();
  }
  if(force||performance.now()-state.lastTable>900){state.lastTable=performance.now();renderTable();renderCorrelation();renderLegend();spark("btc-spark","BTC/USD",colors[0]);spark("eth-spark","ETH/USD",colors[1]);}
  if(state.page==="market")drawMarket();
  if(typeof renderBots==="function")renderBots(data,force);
  if(typeof updateMatrixData==="function")updateMatrixData(data);
}
function renderLegend(){
  const legend=$("chart-legend");legend.replaceChildren();
  state.data.analytics.symbols.forEach((s,i)=>{const label=element("span","legend-item"),dot=element("span","legend-dot");dot.style.background=colors[i];label.append(dot,document.createTextNode(short(s)));legend.append(label);});
}
function renderTable(){
  if(!state.data)return;
  const query=$("asset-search").value.toLowerCase(),d=state.data;
  const assets=d.assets.filter(a=>`${a.symbol} ${a.name}`.toLowerCase().includes(query));
  const tbody=$("asset-rows");tbody.replaceChildren();
  assets.forEach(a=>{
    const tr=element("tr"),td=element("td"),chosen=d.settings.symbols.includes(a.symbol);
    const star=element("button",`star${chosen?" chosen":""}`,chosen?"★":"☆");star.type="button";star.setAttribute("aria-label",`${a.symbol} ${chosen?"tanlovdan olib tashlash":"tahlilga qo‘shish"}`);star.setAttribute("aria-pressed",String(chosen));star.disabled=state.selecting;star.addEventListener("click",()=>toggleSymbol(a.symbol));td.append(star);
    const name=element("td"),cell=element("div","asset-cell"),logo=element("span","coin-logo",short(a.symbol).slice(0,3)),text=element("div");text.append(element("strong",null,a.symbol),element("small",null,a.name));cell.append(logo,text);name.append(cell);
    const quote=a.symbol.split("/")[1];const price=element("td",null,a.price==null?"—":quote==="USD"?money(a.price):`${Number(a.price).toLocaleString("en-US",{maximumFractionDigits:8})} ${quote}`);
    const change=element("td",a.change==null?"":a.change>=0?"positive":"negative",pct(a.change));
    const source=element("td"),badge=element("span","source-badge"),dot=element("span","dot");let label=d.mode==="demo"?"Simulyatsiya":a.price==null?"Narx kutilmoqda":a.stale?"Eskirgan":a.source==="rest"?"Alpaca · REST":"Alpaca · WebSocket";if(!state.transport)label="Server uzilgan";dot.style.background=(!state.transport||a.stale)?"#d6b17d":"#79ac87";badge.append(dot,document.createTextNode(label));source.append(badge);if(a.timestamp)source.title=`Oxirgi kotirovka: ${new Date(a.timestamp*1000).toLocaleString()}`;
    const activeBot=(d.bots||[]).some(b=>b.config.symbol===a.symbol&&b.status==="running"&&b.account_matches);
    const trade=element("td");trade.append(element("span","tradable",d.mode==="demo"?"Demo":a.tradable?activeBot?"Bot faol":"Mavjud":"Mavjud emas"));
    tr.append(td,name,price,change,source,trade);tbody.append(tr);
  });
  if(!assets.length){const tr=element("tr"),td=element("td",null,"Aktiv topilmadi. Qidiruvni o‘zgartiring.");td.colSpan=6;tr.append(td);tbody.append(tr);}
  $("asset-count").textContent=d.assets.length;$("asset-summary").textContent=`${assets.length} / ${d.assets.length} juftlik · ${d.settings.symbols.length} ta tanlangan`;
}
async function toggleSymbol(symbol){
  if(state.selecting)return;
  const selected=state.data.settings.symbols;
  const next=selected.includes(symbol)?selected.filter(s=>s!==symbol):[...selected,symbol];
  if(!next.length||next.length>6){toast("Tahlil uchun 1 tadan 6 tagacha aktiv tanlang.");return;}
  state.selecting=true;
  try{await api("/api/settings",{method:"PUT",body:JSON.stringify({refresh_hz:state.data.settings.refresh_hz,symbols:next})});const data=await api("/api/bootstrap");render(data,true);}catch(e){toast(e.message,true);}finally{state.selecting=false;renderTable();}
}
function renderCorrelation(){
  const {symbols,matrix,samples}=state.data.analytics,grid=$("correlation");grid.replaceChildren();grid.style.gridTemplateColumns=`32px repeat(${symbols.length},minmax(0,1fr))`;grid.append(element("span"));symbols.forEach(s=>grid.append(element("span","corr-label",short(s))));
  symbols.forEach((s,i)=>{grid.append(element("span","corr-label",short(s)));symbols.forEach((t,j)=>{const v=matrix[i][j],e=element("span","corr-cell",v==null?"—":v.toFixed(2));if(v!=null){e.style.background=v>=0?`rgba(147,255,97,${.05+v*.39})`:`rgba(255,126,154,${.08-v*.4})`;e.style.color=v>.65?"#e7ffd3":"#91b87d";}else e.style.background="#102016";e.title=`${s} / ${t}: ${v==null?"Kamida 30 ta bir xil vaqt oralig‘i kerak":v.toFixed(4)}`;grid.append(e);});});
  $("correlation-note").textContent=symbols.length<2?"Bog‘liqlikni ko‘rish uchun kamida 2 ta aktiv tanlang.":`1 s log-daromad · Pearson · ${samples} namuna${samples<30?" / kamida 30 kerak":""}`;
}
function canvasSetup(id){const c=$(id),rect=c.getBoundingClientRect();if(!rect.width||!rect.height)return null;const dpr=Math.min(devicePixelRatio||1,2);if(c.width!==Math.round(rect.width*dpr)||c.height!==Math.round(rect.height*dpr)){c.width=Math.round(rect.width*dpr);c.height=Math.round(rect.height*dpr);}const ctx=c.getContext("2d");ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,rect.width,rect.height);ctx.font="10px Consolas, monospace";return {ctx,w:rect.width,h:rect.height};}
function axes(ctx,w,h,min,max,pad={l:45,r:18,t:22,b:28},n=4){const x0=pad.l,y0=pad.t,pw=w-pad.l-pad.r,ph=h-pad.t-pad.b;ctx.lineWidth=1;for(let i=0;i<=n;i++){const y=y0+ph*i/n;ctx.beginPath();ctx.strokeStyle="#a9ff7812";ctx.setLineDash([3,4]);ctx.moveTo(x0,y);ctx.lineTo(w-pad.r,y);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle="#6d8f61";ctx.textAlign="right";const v=max-(max-min)*i/n;ctx.fillText(`${v.toFixed(2)}%`,x0-8,y+3);}return {x0,y0,pw,ph,y:v=>y0+ph*(max-v)/(max-min)};}
function drawMarket(){
  if(!state.data)return;const setup=canvasSetup("market-chart");if(!setup)return;const {ctx,w,h}=setup,{chart,symbols}=state.data.analytics;
  const values=chart.flatMap(p=>symbols.map(s=>p[s]).filter(v=>v!=null));$("chart-empty").classList.toggle("hidden",values.length>1);
  $("chart-empty").textContent=state.data.stream_error&&state.data.data_source!=="rest"?state.data.stream_error.message:values.length?"Birinchi narx olindi. Grafik uchun keyingi namuna kutilmoqda…":"Narxlar kutilmoqda…";
  if(!values.length)return;
  if(state.chart==="bar"){
    const latest=chart.at(-1),vals=symbols.map(s=>latest?.[s]);const maxAbs=Math.max(.05,...vals.filter(v=>v!=null).map(Math.abs))*1.3;const a=axes(ctx,w,h,-maxAbs,maxAbs);const bw=Math.min(52,a.pw/symbols.length*.4);
    symbols.forEach((s,i)=>{const v=vals[i],x=a.x0+a.pw*(i+.5)/symbols.length;if(v!=null){ctx.fillStyle=colors[i];const y=a.y(v),base=a.y(0);ctx.fillRect(x-bw/2,Math.min(y,base),bw,Math.max(1,Math.abs(base-y)));ctx.fillStyle="#a6d28e";ctx.textAlign="center";ctx.fillText(pct(v),x,v>=0?y-8:y+14);}ctx.fillStyle="#6d8f61";ctx.textAlign="center";ctx.fillText(short(s),x,h-9);});
  }else{
    let min=Math.min(0,...values),max=Math.max(0,...values),range=Math.max(.1,max-min);min-=range*.16;max+=range*.16;const a=axes(ctx,w,h,min,max);
    let tail=[];chart.forEach((p,i)=>{if(p[symbols[0]]==null)tail=[];else tail.push([a.x0+a.pw*i/Math.max(1,chart.length-1),a.y(p[symbols[0]])]);});
    if(tail.length>1){const gradient=ctx.createLinearGradient(0,a.y0,0,a.y0+a.ph);gradient.addColorStop(0,"#bbff6b1b");gradient.addColorStop(1,"#bbff6b00");ctx.beginPath();ctx.moveTo(tail[0][0],a.y0+a.ph);tail.forEach(p=>ctx.lineTo(p[0],p[1]));ctx.lineTo(tail.at(-1)[0],a.y0+a.ph);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();}
    symbols.forEach((s,j)=>{ctx.save();ctx.beginPath();ctx.strokeStyle=colors[j];ctx.lineWidth=j===0?1.9:1.5;ctx.shadowColor=colors[j]+"50";ctx.shadowBlur=7;let started=false;chart.forEach((p,i)=>{if(p[s]==null){started=false;return;}const x=a.x0+a.pw*i/Math.max(1,chart.length-1),y=a.y(p[s]);if(!started){ctx.moveTo(x,y);started=true;}else ctx.lineTo(x,y);});ctx.stroke();ctx.restore();});
    if(tail.length){const [x,y]=tail.at(-1);const halo=ctx.createRadialGradient(x,y,0,x,y,13);halo.addColorStop(0,"#bbff6b30");halo.addColorStop(1,"#bbff6b00");ctx.fillStyle=halo;ctx.beginPath();ctx.arc(x,y,13,0,Math.PI*2);ctx.fill();ctx.fillStyle=colors[0];ctx.beginPath();ctx.arc(x,y,3,0,Math.PI*2);ctx.fill();ctx.strokeStyle="#0b170d";ctx.lineWidth=1.2;ctx.stroke();}
    const count=w<400?3:5;ctx.fillStyle="#6d8f61";ctx.textAlign="center";for(let i=0;i<count;i++){const index=Math.floor((chart.length-1)*i/(count-1)),p=chart[index];if(p)ctx.fillText(new Date(p.time*1000).toLocaleTimeString([],{hour:"2-digit",minute:"2-digit",second:"2-digit"}),a.x0+a.pw*i/(count-1),h-8);}
  }
  $("chart-caption").textContent=state.chart==="bar"?"Kuzatuv oynasi boshidan hozirgacha · %":"Bir xil boshlang‘ich nuqta · o‘zgarish, %";
}
function spark(id,symbol,color){const e=$(id),points=state.data.analytics.chart.map(p=>p[symbol]).filter(v=>v!=null).filter((_,i)=>i%6===0);e.replaceChildren();if(points.length<2)return;const min=Math.min(...points),max=Math.max(...points),range=Math.max(.001,max-min);const line=document.createElementNS("http://www.w3.org/2000/svg","polyline");line.setAttribute("points",points.map((p,i)=>`${170*i/(points.length-1)},${44-(p-min)/range*36}`).join(" "));line.setAttribute("fill","none");line.setAttribute("stroke",color);line.setAttribute("stroke-width","1.5");e.append(line);}
async function loadHistory(){
  if(!state.data||state.page!=="balance")return;const request=++state.historyRequest;
  $("balance-empty").textContent="Balans tarixi yuklanmoqda…";$("balance-empty").classList.remove("hidden");state.history=null;$("history-summary").textContent="—";
  try{const data=await api(`/api/portfolio?period=${state.period}`);if(request!==state.historyRequest)return;state.history=data;$("balance-empty").classList.toggle("hidden",data.points.length>1);if(data.points.length<2)$("balance-empty").textContent="Tanlangan davr uchun yetarli balans tarixi yo‘q.";drawBalance();}catch(e){if(request===state.historyRequest){$("balance-empty").textContent=e.message;$("balance-empty").classList.remove("hidden");}}
}
function drawBalance(){
  const setup=canvasSetup("balance-chart");if(!setup||!state.history)return;const {ctx,w,h}=setup,points=state.history.points;if(points.length<2)return;
  const vals=points.map(p=>p.equity),min=Math.min(...vals),max=Math.max(...vals),range=Math.max(1,max-min),left=63,right=20,top=22,lineH=h*.58,pw=w-left-right;const y=v=>top+lineH*(1-(v-min+range*.12)/(range*1.24));
  ctx.strokeStyle="#a9ff7812";ctx.fillStyle="#6d8f61";ctx.textAlign="right";for(let i=0;i<=4;i++){const yy=top+lineH*i/4;ctx.beginPath();ctx.moveTo(left,yy);ctx.lineTo(w-right,yy);ctx.stroke();ctx.fillText(money(max+range*.12-range*1.24*i/4,state.history.currency),left-8,yy+3);}
  const gradient=ctx.createLinearGradient(0,top,0,top+lineH);gradient.addColorStop(0,"#baff6b22");gradient.addColorStop(1,"#baff6b00");ctx.beginPath();points.forEach((p,i)=>{const x=left+pw*i/(points.length-1);if(i)ctx.lineTo(x,y(p.equity));else ctx.moveTo(x,y(p.equity));});ctx.lineTo(w-right,top+lineH);ctx.lineTo(left,top+lineH);ctx.closePath();ctx.fillStyle=gradient;ctx.fill();ctx.beginPath();points.forEach((p,i)=>{const x=left+pw*i/(points.length-1);i?ctx.lineTo(x,y(p.equity)):ctx.moveTo(x,y(p.equity));});ctx.strokeStyle="#baff6b";ctx.lineWidth=2;ctx.stroke();
  const deltas=points.slice(1).map((p,i)=>p.equity-points[i].equity),abs=Math.max(1,...deltas.map(Math.abs)),base=top+lineH+(h-top-lineH-35)/2+15,bh=(h-top-lineH-45)/2;ctx.lineWidth=1;ctx.strokeStyle="#baff6b15";ctx.beginPath();ctx.moveTo(left,base);ctx.lineTo(w-right,base);ctx.stroke();
  deltas.forEach((v,i)=>{const height=v/abs*bh;ctx.fillStyle=v>=0?"#91d86f":"#c77a89";ctx.fillRect(left+pw*(i+1)/(points.length-1),Math.min(base,base-height),Math.max(1,pw/points.length*.65),Math.max(1,Math.abs(height)));});ctx.fillStyle="#6d8f61";ctx.textAlign="center";const ticks=w<450?3:5;for(let i=0;i<ticks;i++){const p=points[Math.round((points.length-1)*i/(ticks-1))];ctx.fillText(new Date(p.time*1000).toLocaleDateString([],{day:"numeric",month:"short",...(state.period==="1D"?{hour:"2-digit",minute:"2-digit"}:{})}),left+pw*i/(ticks-1),h-10);}
  $("history-summary").textContent=`${state.history.demo?"Namuna · ":""}Davr o‘zgarishi: ${money(vals.at(-1)-vals[0],state.history.currency)}`;
}
function drawAll(){if(state.page==="market")drawMarket();if(state.page==="balance")drawBalance();}
async function connectSocket(){
  if(state.closing)return;
  const ws=new WebSocket(`${location.protocol==="https:"?"wss":"ws"}://${location.host}/ws`);state.socket=ws;
  ws.onopen=()=>{ws.send(JSON.stringify({token:state.token}));};
  ws.onmessage=e=>{try{state.transport=true;render(JSON.parse(e.data));}catch{toast("Ma’lumotni o‘qishda xatolik.",true);}};
  ws.onclose=()=>{state.transport=false;if(state.data)render(state.data,true);if(!state.closing)setTimeout(async()=>{try{const boot=await api("/api/bootstrap");state.token=boot.token;connectSocket();}catch{connectSocket();}},2000);};
  ws.onerror=()=>ws.close();
}
$("connection-form").addEventListener("submit",async e=>{
  e.preventDefault();const button=$("connect-button"),message=$("connection-message");button.disabled=true;button.textContent="Tekshirilmoqda…";message.classList.add("hidden");
  try{const result=await api("/api/connection",{method:"POST",body:JSON.stringify({api_key:$("api-key").value,secret_key:$("secret-key").value,mode:$("connection-mode").value,remember:$("remember-keys").checked})});$("api-key").value="";$("secret-key").value="";message.textContent=result.message;message.classList.remove("error");const boot=await api("/api/bootstrap");render(boot,true);toast(result.message);}catch(e){message.textContent=e.message;message.classList.add("error");}finally{message.classList.remove("hidden");button.disabled=false;button.textContent="Tekshirish va ulash ↗";}
});
$("disconnect-button").addEventListener("click",async()=>{const b=$("disconnect-button");b.disabled=true;try{const result=await api("/api/connection",{method:"DELETE"});render(await api("/api/bootstrap"),true);toast(result.message);}catch(e){toast(e.message,true);}finally{b.disabled=state.data?.mode==="demo";}});
$("connection-mode").addEventListener("change",()=>{$("mode-help").textContent=$("connection-mode").value==="live"?"Live — haqiqiy akkaunt. Har bir bot Trader Bots bo‘limida alohida tasdiq bilan boshlanadi.":"Paper va Live kalitlari alohida. Mos kalitlarni kiriting.";});
document.querySelectorAll("[data-reveal]").forEach(b=>b.addEventListener("click",()=>{const input=$(b.dataset.reveal),show=input.type==="password";input.type=show?"text":"password";b.textContent=show?"Yashirish":"Ko‘rish";b.setAttribute("aria-pressed",String(show));}));
$("refresh-rate").addEventListener("input",e=>{state.rateDirty=true;$("rate-number").replaceChildren(document.createTextNode(e.target.value),element("span",null," Hz"));});
$("save-settings").addEventListener("click",async()=>{const b=$("save-settings");if(!state.data)return;b.disabled=true;try{await api("/api/settings",{method:"PUT",body:JSON.stringify({refresh_hz:Number($("refresh-rate").value),symbols:state.data.settings.symbols})});state.rateDirty=false;render(await api("/api/bootstrap"),true);toast("Panel sozlamalari saqlandi.");}catch(e){toast(e.message,true);}finally{b.disabled=false;}});
$("asset-search").addEventListener("input",renderTable);
document.querySelectorAll("[data-chart]").forEach(b=>b.addEventListener("click",()=>{state.chart=b.dataset.chart;document.querySelectorAll("[data-chart]").forEach(x=>x.classList.toggle("selected",x===b));drawMarket();if(typeof matrixMotion!=="undefined"&&!matrixMotion.reduced)$("market-chart").animate([{opacity:.4,filter:"blur(3px)",transform:"translateY(4px)"},{opacity:1,filter:"blur(0)",transform:"translateY(0)"}],{duration:420,easing:"cubic-bezier(.2,.8,.2,1)"});}));
document.querySelectorAll("[data-period]").forEach(b=>b.addEventListener("click",()=>{state.period=b.dataset.period;document.querySelectorAll("[data-period]").forEach(x=>x.classList.toggle("selected",x===b));loadHistory();}));
window.addEventListener("hashchange",navigate);window.addEventListener("resize",drawAll);window.addEventListener("beforeunload",()=>{state.closing=true;state.socket?.close();});
async function start(){navigate();try{const boot=await api("/api/bootstrap");state.token=boot.token;state.transport=true;render(boot,true);connectSocket();if(state.page==="balance")loadHistory();}catch(e){$("stream-label").textContent="Python server bilan aloqa yo‘q";toast(e.message,true);}}
start();
