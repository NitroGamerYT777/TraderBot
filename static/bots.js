"use strict";
const botUI = {editing:null,action:null,cards:new Map(),busy:new Set()};
const botFields = {name:"bot-name",symbol:"bot-symbol",mode:"bot-mode",timeframe:"bot-timeframe",budget_usd:"bot-budget",max_position_usd:"bot-position",risk_pct:"bot-risk",daily_loss_pct:"bot-daily-loss",stop_loss_pct:"bot-stop",take_profit_pct:"bot-take",max_daily_trades:"bot-trades",cooldown_bars:"bot-cooldown",max_spread_pct:"bot-spread"};
const botDefaults = {name:"",symbol:"BTC/USD",mode:"paper",timeframe:"15Min",budget_usd:100,max_position_usd:25,risk_pct:.5,daily_loss_pct:3,stop_loss_pct:2,take_profit_pct:4,max_daily_trades:4,cooldown_bars:3,max_spread_pct:.5};
function botById(id){return (state.data?.bots||[]).find(b=>b.id===id);}
function formError(id,message){$(id).textContent=message;$(id).classList.toggle("hidden",!message);}
function setValueText(root,selector,value){root.querySelector(selector).textContent=value;}
function makeBotCard(bot){
  const card=element("article","panel trader-bot-card");card.dataset.botId=bot.id;
  card.innerHTML='<div class="trader-card-head"><span class="trader-coin"></span><div class="trader-name"><h2></h2><small></small></div><span class="bot-state-badge"></span><span class="bot-env-badge"></span></div><div class="trader-strategy">Trend Guard <span>EMA 20/50 + RSI</span></div><div class="trader-values"><div><span>Budjet</span><strong class="bot-budget-value"></strong></div><div><span>Ochiq miqdor</span><strong class="bot-qty-value"></strong></div><div><span>Yopilgan P/L · taxmin</span><strong class="bot-pnl-value"></strong></div><div><span>Ochiq P/L · taxmin</span><strong class="bot-open-pnl-value"></strong></div></div><div class="trader-risk-tags"><span class="bot-stop-tag"></span><span class="bot-take-tag"></span><span class="bot-risk-tag"></span></div><p class="bot-current-message"></p><div class="bot-pending-id hidden"></div><div class="trader-card-actions"><button class="button primary" data-bot-action="start">Boshlash</button><button class="button secondary" data-bot-action="pause">Pauza</button><button class="button secondary" data-bot-action="close">Pozitsiyani yopish</button><button class="button secondary" data-bot-action="backtest">Backtest</button><button class="bot-text-button" data-bot-action="events">Jurnal ↗</button><button class="bot-text-button" data-bot-action="edit">Sozlash</button><button class="bot-text-button delete-bot-button" data-bot-action="delete">O‘chirish</button></div>';
  return card;
}
function renderBots(data){
  if(!$("bot-list"))return;
  const bots=data.bots||[],currentMode=data.mode==="live"?"live":"paper",current=bots.filter(b=>b.config.mode===currentMode);
  $("bots-total").textContent=bots.length;
  $("bots-active").textContent=current.filter(b=>b.status==="running").length;
  $("bots-budget").textContent=money(current.reduce((n,b)=>n+b.config.budget_usd,0));
  $("bots-realized").textContent=money(current.reduce((n,b)=>n+b.realized,0));
  $("bots-budget").previousElementSibling.textContent=`Budjetlar · ${currentMode.toUpperCase()}`;
  $("bots-realized").previousElementSibling.textContent=`Yopilgan P/L · ${currentMode.toUpperCase()} · taxmin`;
  $("bots-empty").classList.toggle("hidden",bots.length>0);
  $("create-bot-button").disabled=!!data.bots_error||!Array.isArray(data.bots);
  $("create-first-bot").disabled=$("create-bot-button").disabled;
  if(!Array.isArray(data.bots))$("bots-empty").querySelector("p").textContent="Yangilangan bot funksiyalarini yuklash uchun Python serverini qayta ishga tushiring.";
  for(const [id,card] of botUI.cards){if(!bots.some(b=>b.id===id)){card.remove();botUI.cards.delete(id);}}
  bots.forEach(bot=>{
    let card=botUI.cards.get(bot.id);if(!card){card=makeBotCard(bot);botUI.cards.set(bot.id,card);$("bot-list").prepend(card);}
    const c=bot.config,busy=botUI.busy.has(bot.id),connected=bot.account_matches&&data.mode===c.mode&&state.transport;
    setValueText(card,".trader-coin",short(c.symbol).slice(0,3));setValueText(card,"h2",c.name);setValueText(card,".trader-name small",`${c.symbol} · ${{"5Min":"5 daqiqa","15Min":"15 daqiqa","1Hour":"1 soat"}[c.timeframe]}`);
    const badge=card.querySelector(".bot-state-badge");badge.textContent=bot.pending?"Order kutilmoqda":bot.requires_resume?"Davom ettirish kerak":{draft:"Tayyor",running:"Faol",paused:"Pauza",error:"E’tibor kerak"}[bot.status]||bot.status;badge.className=`bot-state-badge ${bot.status}`;
    const env=card.querySelector(".bot-env-badge");env.textContent=c.mode.toUpperCase();env.classList.toggle("live",c.mode==="live");
    setValueText(card,".bot-budget-value",money(c.budget_usd));setValueText(card,".bot-qty-value",`${Number(bot.qty).toLocaleString("en-US",{maximumSignificantDigits:6})} ${short(c.symbol)}`);
    const realized=card.querySelector(".bot-pnl-value");realized.textContent=money(bot.realized);realized.classList.toggle("positive",bot.realized>=0);realized.classList.toggle("negative",bot.realized<0);
    const unrealized=card.querySelector(".bot-open-pnl-value");unrealized.textContent=bot.qty===0?money(0):bot.quote_stale?"— · narx eski":money(bot.estimated_unrealized);unrealized.classList.toggle("positive",!bot.quote_stale&&bot.estimated_unrealized>=0);unrealized.classList.toggle("negative",!bot.quote_stale&&bot.estimated_unrealized<0);
    setValueText(card,".bot-stop-tag",`SL ${c.stop_loss_pct}%`);setValueText(card,".bot-take-tag",`TP ${c.take_profit_pct}%`);setValueText(card,".bot-risk-tag",`Risk ${c.risk_pct}% · maks. ${money(c.max_position_usd)}`);
    setValueText(card,".bot-current-message",bot.message);
    const pending=card.querySelector(".bot-pending-id");pending.classList.toggle("hidden",!bot.pending);pending.textContent=bot.pending?`Client ID: ${bot.pending.client_order_id}`:"";
    const button=action=>card.querySelector(`[data-bot-action="${action}"]`);
    button("start").classList.toggle("hidden",bot.status==="running");button("start").textContent=bot.status==="draft"?"Boshlash":"Davom ettirish";button("start").disabled=busy||!connected||!!bot.pending||!!data.bots_error;
    button("start").title=connected?"Budjet va riskni ko‘rib, savdoni boshlash":`Account bo‘limida shu botga mos ${c.mode.toUpperCase()} akkauntni ulang`;
    button("pause").classList.toggle("hidden",bot.status!=="running");button("pause").disabled=busy;
    button("close").classList.toggle("hidden",bot.qty<=0);button("close").disabled=busy||!connected||!!bot.pending;
    button("backtest").disabled=busy||data.mode==="demo"||!!data.bots_error;
    button("backtest").title=data.mode==="demo"?"Tarixiy ma’lumot uchun Alpaca’ni ulang":"Haqiqiy tarixiy shamlar, taxminiy komissiya va slippage bilan sinash";
    button("edit").disabled=busy||bot.status==="running"||bot.qty>0||!!bot.pending;
    button("delete").disabled=button("edit").disabled||!!data.bots_error;
  });
}
function updateSizingPreview(){
  const budget=Number($("bot-budget").value),risk=Number($("bot-risk").value),stop=Number($("bot-stop").value),cap=Number($("bot-position").value);
  const amount=Math.max(0,Math.min(cap,budget/1.0035,budget*risk/100/(stop/100+.007)));
  $("bot-sizing-preview").textContent=Number.isFinite(amount)?`Boshlang‘ich order ko‘pi bilan ≈ ${money(amount)}. Risk budjeti ≈ ${money(budget*risk/100)}; komissiya va slippage zaxirasi hisobga olinadi. Haqiqiy zarar gap yoki uzilishda kattaroq bo‘lishi mumkin.`:"Budjet va risk qiymatlarini kiriting.";
}
function openBotForm(bot=null){
  if(!state.data){toast("Server ma’lumoti kutilmoqda.",true);return;}
  botUI.editing=bot?.id||null;$("bot-form").reset();formError("bot-form-error","");
  $("bot-dialog-title").textContent=bot?"Bot sozlamalari":"Yangi Trader Bot";$("save-bot-button").textContent=bot?"Sozlamalarni saqlash":"Botni yaratish ↗";
  const available=state.data.assets.filter(a=>a.symbol.endsWith("/USD")),select=$("bot-symbol");select.replaceChildren();
  available.forEach(a=>{const option=element("option",null,a.symbol);option.value=a.symbol;select.append(option);});
  const values=bot?.config||botDefaults;
  Object.entries(botFields).forEach(([key,id])=>{$(id).value=values[key];});
  if(!select.value&&select.options.length)select.selectedIndex=0;
  $("bot-mode").disabled=!!bot;$("bot-symbol").disabled=!!bot;
  $("bot-form-note").textContent=state.data.mode==="demo"?"Demo aktivlar ro‘yxati. Botni yaratish mumkin; ishga tushirish va backtest uchun haqiqiy Alpaca ulanishi kerak.":"Faqat USD juftliklari. Bot yaratilishi order yubormaydi. Paper va Live alohida kalitlar talab qiladi. Budjet ichki limit; pul subakkauntga o‘tkazilmaydi.";
  updateSizingPreview();$("bot-dialog").showModal();
}
async function refreshBots(){const data=await api("/api/bootstrap");render(data,true);}
function openBotAction(bot,action){
  botUI.action={id:bot.id,action,reviewToken:bot.review_token};$("bot-action-form").reset();formError("bot-action-error","");
  const live=bot.config.mode==="live",close=action==="close",deleting=action==="delete";
  $("bot-action-title").textContent=deleting?"Botni o‘chirish":close?"Pozitsiyani yopish":`${live?"Live":"Paper"} botni boshlash`;
  $("bot-action-eyebrow").textContent=live&&!deleting?"HAQIQIY MABLAG‘ BILAN AMAL":"SAVDONI BOSHQARISH";
  const summary=$("bot-action-summary");summary.replaceChildren(element("h3",null,bot.config.name));
  const details=element("dl","action-summary-grid");
  [["Kripto",bot.config.symbol],["Muhit",bot.config.mode.toUpperCase()],["Budjet",money(bot.config.budget_usd)],["Maks. pozitsiya",money(bot.config.max_position_usd)],["Stop / Take",`${bot.config.stop_loss_pct}% / ${bot.config.take_profit_pct}%`],["Kunlik zarar limiti",`${bot.config.daily_loss_pct}%`]].forEach(([label,value])=>{const pair=element("div");pair.append(element("dt",null,label),element("dd",null,value));details.append(pair);});summary.append(details);
  summary.append(element("p","action-warning",deleting?"Bot va uning lokal jurnali o‘chiriladi. Brokerdagi tarix o‘zgarmaydi.":close?"Botga tegishli miqdor bozor orderi bilan sotiladi. Narx va komissiya yakuniy natijaga ta’sir qiladi.":`${live?"Haqiqiy":"Virtual"} mablag‘ bilan avtomatik xarid va sotish boshlanadi. Foyda kafolatlanmaydi. Server yoki internet uzilsa lokal stop-loss va take-profit ishlamaydi.`));
  $("bot-risk-ack-label").textContent=deleting?"Shu bot va uning lokal jurnalini o‘chirishni tasdiqlayman.":"Budjet va risklarni ko‘rib chiqdim. Lokal himoya uchun server hamda internet ishlashi kerakligini tushundim.";
  const phrase=`${close?"CLOSE":"LIVE"} ${bot.config.name}`;
  $("live-confirm-field").classList.toggle("hidden",!live||deleting);$("live-confirm-label").textContent=`Tasdiqlash uchun yozing: ${phrase}`;$("live-confirmation").required=live&&!deleting;
  $("bot-action-submit").textContent=deleting?"Botni o‘chirish":close?"Pozitsiyani sotib yopish":`${live?"Live":"Paper"} savdoni boshlash`;
  $("bot-action-dialog").showModal();
}
async function showEvents(bot){
  $("bot-details-title").textContent=`${bot.config.name} · Jurnal`;const content=$("bot-details-content");content.replaceChildren(element("p","muted","Jurnal yuklanmoqda…"));$("bot-details-dialog").showModal();
  try{const result=await api(`/api/bots/${bot.id}/events`);content.replaceChildren();if(!result.events.length)content.append(element("p","muted","Jurnal hozircha bo‘sh."));result.events.forEach(event=>{const row=element("div","bot-event");row.append(element("time",null,new Date(event.time*1000).toLocaleString()),element("p",null,event.message));content.append(row);});}catch(e){content.replaceChildren(element("p","negative",e.message));}
}
function showBacktest(bot,result){
  $("bot-details-title").textContent=`${bot.config.name} · Backtest`;const content=$("bot-details-content");content.replaceChildren();
  const metrics=element("div","backtest-metrics");[["Daromad",pct(result.return_pct)],["Maks. drawdown",`${result.max_drawdown_pct}%`],["Yopilgan savdolar",result.trades],["Yutuq ulushi",result.win_rate==null?"—":`${result.win_rate}%`]].forEach(([label,value])=>{const item=element("div");item.append(element("span",null,label),element("strong",null,String(value)));metrics.append(item);});content.append(metrics);
  content.append(element("p","muted",`${result.bars} ta yopilgan sham · ${new Date(result.start*1000).toLocaleString()} — ${new Date(result.end*1000).toLocaleString()}`));
  content.append(element("p","action-warning",result.note));
  if(result.trades<10)content.append(element("p","action-warning","Savdolar soni juda kam: bu natija strategiya samaradorligini baholash uchun yetarli emas."));
  if(result.open_position)content.append(element("p","muted","Davr oxirida ochiq pozitsiya bor; qiymati oxirgi yopilish narxida baholangan."));
  if(result.curve.length>1){const svg=document.createElementNS("http://www.w3.org/2000/svg","svg");svg.setAttribute("viewBox","0 0 600 150");svg.setAttribute("class","backtest-spark");svg.setAttribute("role","img");svg.setAttribute("aria-label","Backtestdagi taxminiy kapital grafigi");const vals=result.curve.map(p=>p.equity),min=Math.min(...vals),range=Math.max(.01,Math.max(...vals)-min),line=document.createElementNS("http://www.w3.org/2000/svg","polyline");line.setAttribute("points",vals.map((v,i)=>`${10+i*580/(vals.length-1)},${135-(v-min)/range*120}`).join(" "));line.setAttribute("fill","none");line.setAttribute("stroke","#258867");line.setAttribute("stroke-width","2");svg.append(line);content.append(svg);}
  const label=element("h3","details-subheading","Oxirgi yopilgan savdolar");content.append(label);
  result.recent_trades.slice().reverse().forEach(t=>{const row=element("div","backtest-trade");row.append(element("span",null,new Date(t.time*1000).toLocaleString()),element("span",null,t.reason),element("strong",t.pnl>=0?"positive":"negative",money(t.pnl)));content.append(row);});
  content.append(element("p","field-help","Drawdown sham yopilishlaridagi kapitalga asoslanadi. Tarixiy sinov haqiqiy order yubormaydi va Live ishlash isboti emas."));
  $("bot-details-dialog").showModal();
}
$("create-bot-button").addEventListener("click",()=>openBotForm());$("create-first-bot").addEventListener("click",()=>openBotForm());
document.querySelectorAll("[data-close-dialog]").forEach(button=>button.addEventListener("click",()=>$(button.dataset.closeDialog).close()));
$("bot-form").addEventListener("input",updateSizingPreview);
$("bot-form").addEventListener("submit",async event=>{
  event.preventDefault();const submit=$("save-bot-button"),config={strategy:"trend_guard"};Object.entries(botFields).forEach(([key,id])=>{config[key]=["name","symbol","mode","timeframe"].includes(key)?$(id).value:Number($(id).value);});submit.disabled=true;formError("bot-form-error","");
  try{const result=await api(botUI.editing?`/api/bots/${botUI.editing}`:"/api/bots",{method:botUI.editing?"PUT":"POST",body:JSON.stringify(config)});$("bot-dialog").close();await refreshBots();toast(result.message);}catch(e){formError("bot-form-error",e.message);}finally{submit.disabled=false;}
});
$("bot-list").addEventListener("click",async event=>{
  const target=event.target.closest("[data-bot-action]");if(!target||target.disabled)return;const id=target.closest("[data-bot-id]").dataset.botId,bot=botById(id),action=target.dataset.botAction;if(!bot)return;
  if(action==="edit"){openBotForm(bot);return;}if(action==="events"){showEvents(bot);return;}if(["start","close","delete"].includes(action)){openBotAction(bot,action);return;}
  botUI.busy.add(id);renderBots(state.data);
  try{const result=await api(`/api/bots/${id}/${action}`,{method:"POST"});await refreshBots();if(action==="backtest")showBacktest(bot,result);else toast(result.message);}catch(e){toast(e.message,true);}finally{botUI.busy.delete(id);renderBots(state.data);}
});
$("bot-action-form").addEventListener("submit",async event=>{
  event.preventDefault();if(!botUI.action)return;const {id,action}=botUI.action,bot=botById(id),submit=$("bot-action-submit");if(!bot)return;submit.disabled=true;formError("bot-action-error","");
  try{const result=await api(`/api/bots/${id}${action==="delete"?"":`/${action}`}`,{method:action==="delete"?"DELETE":"POST",body:action==="delete"?undefined:JSON.stringify({acknowledge_risk:$("bot-risk-ack").checked,confirmation:$("live-confirmation").value,review_token:botUI.action.reviewToken})});$("bot-action-dialog").close();await refreshBots();toast(result.message);}catch(e){formError("bot-action-error",e.message);}finally{submit.disabled=false;}
});
