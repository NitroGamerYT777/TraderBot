"use strict";

// Visual effects use local state only. They never call trading endpoints.
const matrixMotion = (() => {
  const media=matchMedia("(prefers-reduced-motion: reduce)");
  let preference="full";
  try{preference=localStorage.getItem("auto-trader-motion")||"full";}catch{}
  let reduced=media.matches||preference==="reduced";
  let rainFrame=0,lastFrame=0,columns=[],canvasWidth=0,canvasHeight=0;
  const canvas=$("matrix-rain"),context=canvas.getContext("2d",{alpha:true});
  const glyphs="アイウエオカキクケコサシスセソタチツテトナニヌネノ01234578901";
  const mobile=matchMedia("(max-width: 760px)");
  const listeners=[];

  function size(){
    const dpr=Math.min(devicePixelRatio||1,1.5);
    canvasWidth=innerWidth;canvasHeight=innerHeight;
    canvas.width=Math.round(canvasWidth*dpr);canvas.height=Math.round(canvasHeight*dpr);
    context.setTransform(dpr,0,0,dpr,0,0);
    columns=Array.from({length:Math.ceil(canvasWidth/27)},()=>({y:-Math.random()*canvasHeight,speed:14+Math.random()*24,phase:Math.random()*100}));
    context.clearRect(0,0,canvasWidth,canvasHeight);
    if(reduced)still();
  }
  function still(){
    context.clearRect(0,0,canvasWidth,canvasHeight);
    context.font="12px Consolas, monospace";context.fillStyle="#8ee85c55";
    columns.forEach((drop,i)=>{for(let j=0;j<5;j++)context.fillText(glyphs[(i*7+j*3)%glyphs.length],i*27,30+j*31+(i%9)*17);});
  }
  function frame(now){
    rainFrame=0;
    if(reduced||document.hidden)return;
    const period=mobile.matches?65:48;
    if(now-lastFrame>=period){
      const delta=Math.min((now-lastFrame)/1000,.1);lastFrame=now;
      context.fillStyle="rgba(5,9,7,.075)";context.fillRect(0,0,canvasWidth,canvasHeight);
      context.font="12px Consolas, monospace";
      columns.forEach((drop,i)=>{
        drop.y+=drop.speed*delta*4;
        context.fillStyle=i%7===0?"#c8ff9490":"#6dde5755";
        const glyph=glyphs[Math.floor(now/180+drop.phase)%glyphs.length];
        context.fillText(glyph,i*27,drop.y);
        if(drop.y>canvasHeight+25)drop.y=-Math.random()*canvasHeight*.6;
      });
    }
    rainFrame=requestAnimationFrame(frame);
  }
  function run(){cancelAnimationFrame(rainFrame);rainFrame=0;lastFrame=performance.now();if(reduced)still();else if(!document.hidden)rainFrame=requestAnimationFrame(frame);}
  function apply(){
    reduced=media.matches||preference==="reduced";
    document.body.dataset.motion=reduced?"reduced":"full";
    $("motion-toggle").setAttribute("aria-pressed",String(!reduced));
    $("motion-toggle").setAttribute("aria-label",reduced?"To‘liq animatsiyalarni yoqish":"Animatsiyalarni kamaytirish");
    $("motion-toggle").title=reduced?"FX: kamaytirilgan harakat":"FX: to‘liq animatsiyalar";
    if(reduced)document.getAnimations().forEach(animation=>animation.cancel());
    run();listeners.forEach(callback=>callback(reduced));
  }
  $("motion-toggle").addEventListener("click",()=>{
    if(media.matches){toast("Qurilmangizning kamaytirilgan harakat sozlamasi faol.");return;}
    preference=reduced?"full":"reduced";try{localStorage.setItem("auto-trader-motion",preference);}catch{}
    apply();toast(reduced?"FX: sokin rejim":"FX: Matrix rejimi yoqildi");
  });
  media.addEventListener("change",apply);document.addEventListener("visibilitychange",run);
  let resizeFrame=0;window.addEventListener("resize",()=>{cancelAnimationFrame(resizeFrame);resizeFrame=requestAnimationFrame(size);});
  size();apply();
  return {get reduced(){return reduced;},onChange:callback=>listeners.push(callback)};
})();

function matrixPageEnter(){
  if(matrixMotion.reduced)return;
  const page=$("page-"+state.page);if(!page)return;
  page.getAnimations().forEach(animation=>animation.cancel());
  page.animate([{opacity:0,transform:"translateY(9px) scale(.994)",filter:"blur(3px)"},{opacity:1,transform:"translateY(0) scale(1)",filter:"blur(0)"}],{duration:540,easing:"cubic-bezier(.2,.8,.2,1)"});
}

const matrixTapeItems=new Map();
function updateMatrixData(data){
  $("core-state").textContent=data.mode==="demo"?"DEMO":data.status==="connected"?"CONNECTED":data.status==="rest"?"REST LINK":"SYNCING";
  document.querySelector(".matrix-core").classList.toggle("online",data.mode==="demo"||["connected","rest"].includes(data.status));
  $("core-feed").textContent=`FEED / ${data.mode==="demo"?"SIMULATED":(data.data_source||"WAITING").toUpperCase()}`;
  $("core-assets").textContent=`${data.assets.length.toString().padStart(2,"0")} ASSETS`;
  ["BTC/USD","ETH/USD","SOL/USD","AVAX/USD","DOGE/USD","LINK/USD"].forEach(symbol=>{
    let item=matrixTapeItems.get(symbol);
    if(!item){item=element("div","tape-item");item.append(element("span","tape-symbol",symbol),element("span","tape-price","—"),element("span","tape-change","—"));matrixTapeItems.set(symbol,item);$("market-tape").append(item);}
    const asset=data.assets.find(a=>a.symbol===symbol);
    item.querySelector(".tape-price").textContent=money(asset?.price);
    const change=item.querySelector(".tape-change");change.textContent=pct(asset?.change);change.classList.toggle("positive",asset?.change>=0);change.classList.toggle("negative",asset?.change<0);
    item.classList.toggle("stale",asset?.stale||!asset?.price);
    item.title=data.mode==="demo"?"Simulyatsiya":asset?.stale?"Oxirgi narx eskirgan":`${symbol} · ${asset?.source==="rest"?"REST":"WebSocket"}`;
  });
}

document.querySelectorAll("[data-scroll-to]").forEach(button=>button.addEventListener("click",()=>$(button.dataset.scrollTo)?.scrollIntoView({behavior:matrixMotion.reduced?"instant":"smooth",block:"start"})));

// Intersection reveals happen once per element, not on every market-data refresh.
const matrixReveal=new IntersectionObserver(entries=>{
  entries.forEach(entry=>{
    if(!entry.isIntersecting)return;
    matrixReveal.unobserve(entry.target);
    if(matrixMotion.reduced)return;
    entry.target.animate([{opacity:0,transform:"translateY(16px)"},{opacity:1,transform:"translateY(0)"}],{duration:650,delay:Number(entry.target.dataset.revealDelay||0),easing:"cubic-bezier(.18,.85,.2,1)"});
  });
},{threshold:.08});
document.querySelectorAll(".metric-card,.analysis-grid>.panel,.assets-panel,.bot-stat,.strategy-explainer,.settings-panel,.connection-form").forEach((card,i)=>{card.dataset.revealDelay=String((i%3)*65);matrixReveal.observe(card);});

document.querySelectorAll("dialog").forEach(dialog=>{
  new MutationObserver(()=>{
    if(!dialog.open||matrixMotion.reduced)return;
    // Financial confirmation targets stay fixed; only their opacity changes.
    const keys=dialog.classList.contains("action-dialog")?[{opacity:0},{opacity:1}]:[{opacity:0,transform:"translateY(16px) scale(.975)",filter:"blur(4px)"},{opacity:1,transform:"translateY(0) scale(1)",filter:"blur(0)"}];
    dialog.animate(keys,{duration:dialog.classList.contains("action-dialog")?180:480,easing:"cubic-bezier(.2,.85,.2,1.1)"});
  }).observe(dialog,{attributes:true,attributeFilter:["open"]});
});

document.addEventListener("click",event=>{
  if(matrixMotion.reduced||event.detail===0)return;
  const target=event.target.closest(".button,.motion-toggle");if(!target||target.disabled)return;
  const rect=target.getBoundingClientRect(),ripple=element("span","ripple");ripple.setAttribute("aria-hidden","true");ripple.style.left=`${event.clientX-rect.left}px`;ripple.style.top=`${event.clientY-rect.top}px`;target.append(ripple);ripple.addEventListener("animationend",()=>ripple.remove(),{once:true});
  setTimeout(()=>ripple.remove(),800);
});

if(matchMedia("(pointer: fine)").matches){
  let pointerFrame=0,pointerX=0,pointerY=0;
  document.addEventListener("pointermove",event=>{
    if(matrixMotion.reduced||document.hidden)return;pointerX=event.clientX;pointerY=event.clientY;
    if(!pointerFrame)pointerFrame=requestAnimationFrame(()=>{$("cursor-aura").style.transform=`translate(${pointerX-260}px,${pointerY-260}px)`;pointerFrame=0;});
  },{passive:true});
  document.querySelectorAll(".metric-card,.matrix-core").forEach(card=>{
    let rect=null,frame=0,x=0,y=0;
    card.addEventListener("pointerenter",()=>{rect=card.getBoundingClientRect();});
    card.addEventListener("pointermove",event=>{
      if(matrixMotion.reduced||!rect)return;x=event.clientX-rect.left;y=event.clientY-rect.top;
      if(!frame)frame=requestAnimationFrame(()=>{card.style.setProperty("--spot-x",`${x}px`);card.style.setProperty("--spot-y",`${y}px`);card.style.transform=`perspective(900px) rotateX(${(rect.height/2-y)/rect.height*3}deg) rotateY(${(x-rect.width/2)/rect.width*3}deg) translateY(-2px)`;frame=0;});
    },{passive:true});
    card.addEventListener("pointerleave",()=>{cancelAnimationFrame(frame);frame=0;rect=null;card.style.transform="";});
    matrixMotion.onChange(()=>{cancelAnimationFrame(frame);frame=0;card.style.transform="";});
  });
}

document.querySelectorAll("#btc-price,#eth-price,#portfolio-value,#balance-equity").forEach(value=>{
  let previous=value.textContent,lastPulse=0;
  new MutationObserver(()=>{
    const current=value.textContent;if(current===previous)return;previous=current;
    if(matrixMotion.reduced||document.hidden||performance.now()-lastPulse<1800)return;
    lastPulse=performance.now();value.classList.remove("value-pulse");requestAnimationFrame(()=>value.classList.add("value-pulse"));
  }).observe(value,{childList:true,characterData:true,subtree:true});
});
if(state.data)updateMatrixData(state.data);
matrixPageEnter();
