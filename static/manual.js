"use strict";
const manualUI={assets:"",mode:null,review:null,deadline:0,busy:false,loading:false,snapshot:null};
function selectManualSymbol(symbol){$("manual-symbol").value=symbol;updateManualQuote();}
function renderManual(data){
  const signature=data.assets.filter(a=>a.symbol.endsWith("/USD")).map(a=>a.symbol).join(",");
  if(signature!==manualUI.assets){
    const old=$("manual-symbol").value;$("manual-symbol").replaceChildren();
    data.assets.filter(a=>a.symbol.endsWith("/USD")).forEach(a=>{const opt=element("option",null,`${a.symbol} · ${a.name}`);opt.value=a.symbol;$("manual-symbol").append(opt);});
    if(data.assets.some(a=>a.symbol===old))$("manual-symbol").value=old;
    manualUI.assets=signature;
  }
  $("manual-mode").textContent=data.mode.toUpperCase();
  const connection=`${data.mode}:${data.account?.id||""}`;
  if(manualUI.mode!==connection){
    manualUI.mode=connection;manualUI.snapshot=null;manualUI.review=null;
    $("manual-dialog").close();renderManualSnapshot(null);
    if(state.page==="trade")loadManual();
  }
  updateManualQuote();
}
function updateManualQuote(){
  if(!state.data)return;
  const a=state.data.assets.find(a=>a.symbol===$("manual-symbol").value),buy=$("manual-side").value==="buy",amount=Number($("manual-amount").value);
  $("manual-bid").textContent=money(a?.bid);$("manual-ask").textContent=money(a?.ask);
  const bot=(state.data.bots||[]).find(b=>b.account_matches&&b.config.symbol===a?.symbol&&(b.status==="running"||b.qty>0||b.pending));
  const blocked=!state.transport||state.data.mode==="demo"||!a?.tradable||a?.stale||!a?.bid||!a?.ask||!!bot;
  $("manual-quote-status").textContent=bot?`Bu kripto «${bot.config.name}» botiga tegishli. Avval bot bo‘limida uni yakunlang.`:state.data.mode==="demo"?"DEMO · Savdo uchun Alpaca Paper yoki Live akkauntini ulang.":!state.transport?"Server bilan aloqa uzilgan.":a?.stale?"Kotirovka eskirgan. Yangi narxni kuting.":`Spread ≈ ${a?.ask&&a?.bid?((a.ask/a.bid-1)*100).toFixed(3):"—"}% · ${a?.source==="rest"?"REST":"WebSocket"}`;
  $("manual-preview-button").disabled=blocked||manualUI.busy;
  $("manual-amount").max=buy?"100000":"1000000000000000000";
  $("manual-amount-label").textContent=buy?"Xarid summasi · USD":`Sotuv miqdori · ${short(a?.symbol||"Kripto/USD")}`;
  $("manual-estimate").textContent=buy?`Miqdor ≈ ${a?.ask&&amount>0?(amount/a.ask).toLocaleString("en-US",{maximumSignificantDigits:8}):"—"} ${short(a?.symbol||"Kripto/USD")}; komissiya uchun qo‘shimcha mablag‘ kerak.`:`Taxminiy tushum ≈ ${money(a?.bid&&amount>0?amount*a.bid:null)} · komissiyadan oldin.`;
  if(manualUI.review)$("manual-submit").disabled=manualUI.busy||!state.transport||manualUI.review.mode!==state.data.mode||performance.now()>manualUI.deadline;
}
function manualLine(root,title,detail,tail){
  const line=element("div","manual-row"),copy=element("div");copy.append(element("strong",null,title),element("small",null,detail));line.append(copy,element("span",null,tail));root.append(line);
}
function renderManualSnapshot(data){
  ["manual-positions","manual-open-orders","manual-history"].forEach(id=>$(id).replaceChildren());
  $("manual-cash").textContent=data&&data.mode!=="demo"?`${data.mode.toUpperCase()} · naqd balans ${money(data.cash)} · botlar zaxirasidan oldin`:"Account bo‘limida Alpaca’ni ulang.";
  const positions=data?.positions||[],orders=data?.open_orders||[],history=data?.orders||[];
  positions.forEach(p=>manualLine($("manual-positions"),p.symbol,`Miqdor ${p.qty} · erkin ${p.qty_available??p.qty} · o‘rtacha ${money(p.avg_entry_price)}`,`${money(p.market_value)} · ochiq P/L ${money(p.unrealized_pl)}`));
  orders.forEach(o=>manualLine($("manual-open-orders"),`${o.symbol} · ${o.side==="buy"?"Xarid":"Sotuv"}`,`${o.type} · ${o.qty} · fill ${o.filled_qty}`,o.status));
  history.forEach(o=>manualLine($("manual-history"),`${o.symbol} · ${o.side==="buy"?"Xarid":"Sotuv"} · ${o.mode.toUpperCase()}`,`Client ID: manual-${o.id} · ${o.message}`,`${o.broker_status||o.status} · fill ${o.filled_qty}/${o.qty}${o.filled_avg_price?` @ ${money(o.filled_avg_price)}`:""}`));
  [["manual-positions",positions,"Ochiq pozitsiya yo‘q."],["manual-open-orders",orders,"Ochiq order yo‘q."],["manual-history",history,"Dastur orqali qo‘lda order yuborilmagan."]].forEach(([id,rows,text])=>{if(!rows.length)$(id).append(element("p","muted",text));});
}
async function loadManual(){
  if(manualUI.loading||!state.token)return;
  manualUI.loading=true;const mode=state.data?.mode;
  try{const data=await api("/api/manual");if(state.data?.mode!==mode)return;manualUI.snapshot=data;renderManualSnapshot(data);$("manual-updated").textContent=`Tekshirildi: ${new Date().toLocaleTimeString()} · Alpaca holati. Qo‘lda pozitsiyalarni o‘zingiz boshqarasiz.`;}
  catch(e){$("manual-updated").textContent=`Yangilash bajarilmadi: ${e.message} Oldingi ma’lumot eskirgan bo‘lishi mumkin.`;}
  finally{manualUI.loading=false;}
}
$("manual-symbol").addEventListener("change",updateManualQuote);
$("manual-side").addEventListener("change",()=>{$("manual-amount").value=$("manual-side").value==="buy"?25:"";updateManualQuote();});
$("manual-amount").addEventListener("input",updateManualQuote);
$("manual-refresh").addEventListener("click",loadManual);
$("manual-form").addEventListener("submit",async event=>{
  event.preventDefault();if(manualUI.busy)return;manualUI.busy=true;formError("manual-error","");updateManualQuote();
  try{
    const ticket={symbol:$("manual-symbol").value,side:$("manual-side").value,amount:Number($("manual-amount").value),mode:state.data.mode};
    const r=await api("/api/manual/preview",{method:"POST",body:JSON.stringify(ticket)});
    if(state.data.mode!==r.mode)throw Error("Muhit o‘zgargan. Orderni qayta ko‘rib chiqing.");
    manualUI.review=r;manualUI.deadline=performance.now()+Math.min(30,r.expires_at-r.created_at)*1000;
    $("manual-expiry").textContent="Ko‘rib chiqish muddati: 30 soniya. 1% dan katta narx o‘zgarishida qayta hisoblash kerak.";
    $("manual-confirm-form").reset();formError("manual-confirm-error","");const root=$("manual-review");root.replaceChildren();
    [["Muhit",r.mode.toUpperCase()],["Amal",r.side==="buy"?"XARID":"SOTUV"],["Kripto",r.symbol],["Miqdor",r.qty],["Hisoblash narxi",money(r.price)],["Taxminiy qiymat",money(r.estimated_value)],["Taxminiy komissiya · 0.25%",money(r.estimated_fee)],["Order turi","Market · GTC"]].forEach(([label,value])=>manualLine(root,label,"",value));
    $("manual-live-field").classList.toggle("hidden",r.mode!=="live");$("manual-phrase").required=r.mode==="live";$("manual-phrase-label").textContent=`Yozing: LIVE ${r.side.toUpperCase()} ${r.symbol}`;
    $("manual-submit").textContent=`${r.mode.toUpperCase()} · ${r.side==="buy"?"Xaridni":"Sotuvni"} tasdiqlash`;
    $("manual-dialog").showModal();
  }catch(e){formError("manual-error",e.message);}finally{manualUI.busy=false;updateManualQuote();}
});
$("manual-confirm-form").addEventListener("submit",async event=>{
  event.preventDefault();const review=manualUI.review;if(!review||manualUI.busy)return;
  manualUI.busy=true;$("manual-submit").disabled=true;formError("manual-confirm-error","");
  try{
    const result=await api(`/api/manual/${review.id}/submit`,{method:"POST",body:JSON.stringify({acknowledge_risk:$("manual-ack").checked,confirmation:$("manual-phrase").value})});
    $("manual-dialog").close();manualUI.review=null;toast(result.message,result.status==="uncertain"||result.status==="rejected");await loadManual();
  }catch(e){formError("manual-confirm-error",`${e.message} Aloqa uzilgan bo‘lsa «Yangilash» orqali jurnalni tekshiring; shu tasdiq bir xil Client ID bilan qayta tekshiriladi.`);}
  finally{manualUI.busy=false;updateManualQuote();}
});
setInterval(()=>{
  if(manualUI.review){const seconds=Math.max(0,Math.ceil((manualUI.deadline-performance.now())/1000));$("manual-expiry").textContent=seconds?`Ko‘rib chiqish muddati: ${seconds} soniya. 1% dan katta narx o‘zgarishida qayta hisoblash kerak.`:"Muddati tugadi. Oynani yopib, orderni qayta ko‘rib chiqing.";updateManualQuote();}
},1000);
setInterval(()=>{if(state.page==="trade"&&state.transport)loadManual();},10000);
