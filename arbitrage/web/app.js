"use strict";
const ScannerTable=(()=>{
  'use strict';
  function patch(node,next){
    if(node.nodeType!==next.nodeType||node.nodeName!==next.nodeName){node.replaceWith(next.cloneNode(true));return;}
    if(node.nodeType===3){if(node.nodeValue!==next.nodeValue)node.nodeValue=next.nodeValue;return;}
    if(node.nodeType!==1)return;
    for(const attribute of Array.from(node.attributes))if(!next.hasAttribute(attribute.name))node.removeAttribute(attribute.name);
    for(const attribute of Array.from(next.attributes))if(node.getAttribute(attribute.name)!==attribute.value)node.setAttribute(attribute.name,attribute.value);
    for(let index=0;index<next.childNodes.length;index++){
      const child=node.childNodes[index],wanted=next.childNodes[index];
      if(child)patch(child,wanted);else node.appendChild(wanted.cloneNode(true));
    }
    while(node.childNodes.length>next.childNodes.length)node.lastChild.remove();
  }
  function update(body,html){
    const draft=body.ownerDocument.createElement(body.localName);draft.innerHTML=html;
    const previous=new Map(Array.from(body.children,row=>[row.dataset.rowKey,row]));
    const wanted=new Set();let cursor=body.firstElementChild;
    const focus=body.contains(body.ownerDocument.activeElement)?body.ownerDocument.activeElement:null;
    for(const next of Array.from(draft.children)){
      const key=next.dataset.rowKey;wanted.add(key);
      let row=previous.get(key);
      if(row)patch(row,next);else row=next;
      if(row!==cursor)body.insertBefore(row,cursor);
      cursor=row.nextElementSibling;
    }
    for(const row of Array.from(body.children))if(!wanted.has(row.dataset.rowKey))row.remove();
    if(focus?.isConnected&&body.ownerDocument.activeElement!==focus)focus.focus({preventScroll:true});
  }
  return {update};
})();

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
const names = {binanceusdm:"Binance",bybit:"Bybit",okx:"OKX",bitget:"Bitget",gate:"Gate",mexc:"MEXC"};
const marks = {binanceusdm:"B",bybit:"BY",okx:"OK",bitget:"b",gate:"G",mexc:"M"};
const reasons = {
  awaiting_depth:"Ожидание стакана",depth_stale:"Стакан устарел",depth_time_skew:"Расходятся времена стаканов",
  volume_24h_unknown:"Нет подтверждённого оборота",volume_24h_stale:"Оборот устарел",volume_24h_below_threshold:"Оборот ниже порога",
  funding_missing:"Нет ставки funding",funding_stale:"Funding устарел",funding_history_incomplete:"Недостаточно истории funding",
  funding_schedule_unknown_or_expired:"Расписание funding неизвестно или истекло",insufficient_budget_utilization:"Шаг количества слишком крупный для бюджета",
  no_feasible_common_quantity:"Нет допустимого общего количества",conditional_net_not_above_threshold:"Условный net не выше порога",
  monitor_stopped:"Монитор остановлен",selection_changed:"Смена отбора глубины",quote_unavailable_or_ineligible:"Потерян допуск котировок",
  previous_session_interrupted:"Предыдущая сессия прервана",depth_source_time_regressed:"Время источника движется назад",
  economic_scenario_incomplete:"Расчёт неполон",confirmation_gap_exceeded:"Разрыв подтверждений",publication_clock_not_advancing:"Нарушена последовательность времени"
};
const eventNames = {appeared:"Кандидат появился",qualifying:"Начата серия подтверждений",confirmed:"Условный сценарий подтверждён",lost:"Условия потеряны",reason_changed:"Изменилась причина",continuity_reset:"Серия сброшена",ended:"Эпизод завершён"};
function stored(key, fallback) {try {return JSON.parse(localStorage.getItem(key)) ?? fallback;} catch {return fallback;}}
const prefs = stored("arbitrage.view", {});
let favorites = new Set(stored("arbitrage.favorites", []));
let data = null, connected = true, offset = 0, selected = null, detailData = null, selectedMissing = false;
let sort = prefs.sort || "open_spread_pct", order = prefs.order || -1, favoriteOnly = false;
let exchanges = new Set(Object.keys(names)), tab = "spread", hovered = null, inflight = false, detailRequest = 0;
let detailTimer = 0, detailLoading = false, renderedRows = null;
let fundingView = "compare", fundingLimit = 50;
let chartEnd=null,chartExpanded=false,chartSeries=[];
const chartDefaults={height:210,fundingHeight:110,extremes:true,grid:true,subplot:true};
let chartPrefs={...chartDefaults,...stored('arbitrage.charts',{})};
if(![210,300,420,560].includes(chartPrefs.height))chartPrefs.height=210;
if(![110,160,220].includes(chartPrefs.fundingHeight))chartPrefs.fundingHeight=110;
for(const key of ['extremes','grid','subplot'])chartPrefs[key]=chartPrefs[key]!==false;
let alertsOpen=false,alertsBusy=false;
let alerts={scope:null,rows:[],cursor:0,latest:0,read:0,unread:0,older:false,moreNew:false,error:null};
function alertReadKey(scope){return 'arbitrage.alertRead.'+scope;}
function savedAlertRead(scope){const value=stored(alertReadKey(scope),0);return Number.isSafeInteger(value)&&value>=0?value:0;}
function renderAlerts(){
  $('unreadAlerts').textContent=alerts.unread>999?'999+':alerts.unread;$('unreadAlerts').classList.toggle('hidden',!alerts.unread);
  $('openAlerts').classList.toggle('alerts-error',!!alerts.error);$('openAlerts').title=alerts.error||`${alerts.unread} непрочитанных`;
  $('markAlertsRead').disabled=alertsBusy||!!alerts.error||!alerts.unread;
  $('alertsStatus').textContent=alerts.error||(!alerts.scope?'Загрузка журнала…':`${alerts.unread} непрочитанных · загружено ${alerts.rows.length}${alerts.moreNew?' · новые записи ещё загружаются':''}`);
  const only=$('onlyUnreadAlerts').checked,rows=[...alerts.rows].reverse().filter(row=>!only||row.id>alerts.read);
  const html=rows.map(row=>{const unread=row.id>alerts.read,current=data?.rows.find(x=>x.id===row.pair_id),parts=row.pair_id.split('|');
    const venue=part=>names[part?.split(':')[0]]||part?.split(':')[0]||'—';
    const title=row.kind==='opened'?'Условный сценарий подтверждён':'Условия сигнала потеряны';
    const direction=current?`${current.symbol} · ${names[current.legs.long.exchange]} LONG → ${names[current.legs.short.exchange]} SHORT`:`${parts[0]} · ${venue(parts[1])} LONG → ${venue(parts[2])} SHORT`;
    return `<article class="alert-item ${unread?'alert-unread':''}" data-row-key="${row.id}"><div class="alert-item-top"><strong>${title}</strong><span>${unread?'● Непрочитано':'Прочитано'}</span></div><div class="alert-direction">${esc(direction)}</div><time>${date(row.timestamp_ms)}</time><div class="alert-net">Условный net при событии: ${money(row.conditional_net_usdt)} USDT</div>${row.reasons?.length?`<div class="alert-reason">${row.reasons.map(x=>esc(reason(x))).join(' · ')}</div>`:''}${current?`<button class="secondary-button" data-alert-pair="${esc(row.pair_id)}">Открыть связку</button>`:'<span class="alert-unavailable">Направление отсутствует в текущем снимке</span>'}</article>`;
  }).join('');
  ScannerTable.update($('alertsList'),html);
  $('alertsEmpty').classList.toggle('hidden',rows.length>0||!!alerts.error||!alerts.scope);
  $('alertsEmpty').textContent=only?'Нет непрочитанных среди загруженных записей.':'В журнале пока нет уведомлений. Они появятся после подтверждения условного сценария.';
  $('olderAlerts').classList.toggle('hidden',!alerts.older||alerts.rows.length>=500);$('olderAlerts').disabled=alertsBusy;
  if(alerts.readError)$('alertsStatus').textContent+=' · отметка прочтения сохранена только до закрытия страницы';
  if(alerts.rows.length>=500)$('alertsStatus').textContent+=' · показаны последние 500; полный журнал доступен через CLI';
}
async function refreshAlerts(older=false){
  if(alertsBusy)return;alertsBusy=true;renderAlerts();
  try{
    let params=new URLSearchParams({limit:'50',read_through:String(alerts.read)});
    if(alerts.scope){if(older&&alerts.rows.length)params.set('before_id',String(alerts.rows[0].id));else params.set('after_id',String(alerts.cursor));}
    let feed=await get('/api/alerts?'+params);
    if(typeof feed.scope!=='string')throw new Error('Перезапустите сервер UI для загрузки журнала.');
    if(feed.scope!==alerts.scope||feed.latest_id<alerts.cursor){
      let read=feed.scope===alerts.scope?0:savedAlertRead(feed.scope);if(read>feed.latest_id)read=0;
      alerts={scope:feed.scope,rows:[],cursor:0,latest:0,read,unread:0,older:false,moreNew:false,error:null};
      params=new URLSearchParams({limit:'50',read_through:String(read)});feed=await get('/api/alerts?'+params);older=false;
    }
    if(feed.scope!==alerts.scope)throw new Error('Источник журнала изменился. Повторите обновление.');
    const combined=new Map(alerts.rows.map(row=>[row.id,row]));for(const row of feed.rows)combined.set(row.id,row);
    alerts.rows=[...combined.values()].sort((a,b)=>a.id-b.id).slice(-500);
    alerts.cursor=Math.max(alerts.cursor,...feed.rows.map(row=>row.id));alerts.latest=feed.latest_id;alerts.unread=feed.unread_count;alerts.error=null;
    if(!older)alerts.moreNew=feed.has_newer;
    if(older||alerts.rows.length===feed.rows.length)alerts.older=feed.has_older;
  }catch(error){alerts.error='Журнал временно недоступен. '+(error.message.includes('Перезапустите')?error.message:'Повторите обновление.');}
  finally{alertsBusy=false;renderAlerts();}
}
function openAlerts(){PaperPanel.close(false);if(selected)closeDetail();alertsOpen=true;$('alertsPanel').classList.remove('hidden');$('alertsBackdrop').classList.remove('hidden');$('openAlerts').setAttribute('aria-expanded','true');document.body.style.overflow='hidden';$('closeAlerts').focus();refreshAlerts();}
function closeAlerts(){alertsOpen=false;$('alertsPanel').classList.add('hidden');$('alertsBackdrop').classList.add('hidden');$('openAlerts').setAttribute('aria-expanded','false');document.body.style.overflow='';$('openAlerts').focus();}
const now = () => Date.now()+offset;
function savePrefs() {try {localStorage.setItem("arbitrage.view",JSON.stringify({sort,order,mode:$("mode").value,hidden:[...document.querySelectorAll("[data-column]")].filter(x=>!x.checked).map(x=>x.dataset.column)}));} catch {}}
function num(value, digits=2, signed=false) {if(value===null||value===undefined||!Number.isFinite(Number(value)))return "—";const n=Number(value);return (signed&&n>0?"+":"")+n.toLocaleString("en-US",{minimumFractionDigits:digits,maximumFractionDigits:digits});}
const pct = (value,digits=3) => value===null||value===undefined?"—":num(value,digits,true)+"%";
const money = value => value===null||value===undefined?"—":num(value,2,true);
function price(value) {if(value===null||value===undefined)return "—";return Number(value).toLocaleString("en-US",{maximumFractionDigits:Math.abs(value)<1?8:Math.abs(value)<100?5:2});}
const date = (stamp,short=false) => stamp?new Intl.DateTimeFormat("ru-RU",{timeZone:"Europe/Kyiv",...(short?{}:{day:"2-digit",month:"2-digit"}),hour:"2-digit",minute:"2-digit",second:"2-digit"}).format(new Date(stamp)):"—";
function ago(ms){if(ms<0)return "0с";if(ms<60000)return Math.floor(ms/1000)+"с";if(ms<3600000)return Math.floor(ms/60000)+"м "+Math.floor(ms%60000/1000)+"с";return Math.floor(ms/3600000)+"ч "+Math.floor(ms%3600000/60000)+"м";}
function period(ms){return ms?ms>=3600000?num(ms/3600000,0)+"ч":num(ms/60000,0)+"м":"—";}
function countdown(stamp){if(!stamp)return "—";const reference=data?.mode==="live"?now():data?.generated_at_ms;const sec=Math.floor((stamp-reference)/1000);if(sec<0)return "истекло";return [Math.floor(sec/3600),Math.floor(sec%3600/60),sec%60].map(x=>String(x).padStart(2,"0")).join(":");}
const signClass = value => value===null||value===undefined?"":value>0?"positive":value<0?"negative-text":"";
const reason = value => reasons[value] || value.replaceAll("_"," ");
function badge(value,formatter=pct){return `<span class="badge ${value===null||value===undefined?"unknown":value<0?"negative":value===0?"neutral":""}">${formatter(value)}</span>`;}
function mark(exchange){return `<span class="exchange-mark ${esc(exchange)}" aria-hidden="true">${esc(marks[exchange]||"·")}</span>`;}
function isFresh(row){return connected&&data?.mode==="live"&&row.data_fresh&&Object.values(row.legs).every(x=>x.source_time_ms!=null&&x.received_at_ms!=null&&now()-x.source_time_ms>=-1000&&now()-x.source_time_ms<=3000&&now()-x.received_at_ms>=-1000&&now()-x.received_at_ms<=3000);}
function modelFresh(row){return isFresh(row)&&row.economic_fresh&&Object.values(row.economics?.depth_source_times||{}).every(x=>now()-x>=-1000&&now()-x<=3000)&&(!(row.economics?.hold_hours>0)||Object.values(row.legs).every(x=>x.funding_received_at_ms!=null&&now()-x.funding_received_at_ms>=-1000&&now()-x.funding_received_at_ms<=120000));}
function status(row){
  if(!connected)return {label:"Нет обновлений",sub:"Связь с сервером потеряна",tone:"bad"};
  if(data.mode!=="live")return {label:"Архив",sub:row.economics?.reasons?.length?reason(row.economics.reasons[0]):row.economics?.status==="scenario_estimated"?"Условная оценка в снимке":"Последнее наблюдение",tone:"warn"};
  if(!isFresh(row))return {label:"Просрочено",sub:"Котировки старше 3 секунд",tone:"warn"};
  if(row.confirmed_current&&modelFresh(row))return {label:"Подтверждено",sub:"Условный сценарий",tone:"good"};
  if(row.economics?.status==="scenario_estimated")return {label:modelFresh(row)?"Рассчитано":"Расчёт устарел",sub:"Будущее — предположение",tone:modelFresh(row)?"":"warn"};
  if(row.economics?.reasons?.length)return {label:"Блокировка",sub:reason(row.economics.reasons[0]),tone:"bad"};
  return {label:"Сырой спред",sub:"Без проверки глубины",tone:""};
}
function toggleFavorite(id){favorites.has(id)?favorites.delete(id):favorites.add(id);try{localStorage.setItem("arbitrage.favorites",JSON.stringify([...favorites]));}catch{}renderTable();if(selected===id)$("detailFavorite").textContent=favorites.has(id)?"★":"☆";}
function resetFilters(){ $("search").value="";$("minSpread").value="";$("minNet").value="";$("readiness").value="all";favoriteOnly=false;exchanges=new Set(Object.keys(names));document.querySelectorAll("[data-exchange-choice]").forEach(x=>x.checked=true);$("favoriteOnly").setAttribute("aria-pressed","false");renderTable();}
function value(row,key){return key==="net"?row.economics?.convergence_net_scenario_usdt:row[key];}
function renderTable(){
  if(!data)return;
  const query=$("search").value.trim().toLowerCase(), minSpread=$("minSpread").value, minNet=$("minNet").value, readiness=$("readiness").value;
  let rows=data.rows.filter(row=>exchanges.has(row.legs.long.exchange)&&exchanges.has(row.legs.short.exchange)
    &&(!query||[row.symbol,row.asset,row.legs.long.native_id,row.legs.short.native_id,names[row.legs.long.exchange],names[row.legs.short.exchange]].some(x=>String(x).toLowerCase().includes(query)))
    &&(!favoriteOnly||favorites.has(row.id))&&(minSpread===""||row.open_spread_pct>=Number(minSpread))
    &&(minNet===""||(row.economics?.convergence_net_scenario_usdt!=null&&row.economics.convergence_net_scenario_usdt>=Number(minNet)))
    &&(readiness==="all"||readiness==="estimated"&&row.economics?.status==="scenario_estimated"||readiness==="confirmed"&&row.confirmed_current&&modelFresh(row)||readiness==="blocked"&&row.economics?.reasons?.length));
  if($("mode").value==="economics")rows=rows.filter(x=>x.economics);
  const counts=new Map();for(const row of rows)counts.set(row.asset,(counts.get(row.asset)||0)+1);
  rows.sort((a,b)=>{const x=value(a,sort),y=value(b,sort);if(x==null||y==null)return x==null&&y==null?a.id.localeCompare(b.id):x==null?1:-1;return (typeof x==="string"?x.localeCompare(y):x-y)*order||a.id.localeCompare(b.id);});
  if($("mode").value==="best"){const seen=new Set();rows=rows.filter(x=>!seen.has(x.asset)&&seen.add(x.asset));}
  $("rowCount").textContent=`${rows.length} из ${data.rows.length} связок`;
  const filterCount=Number(minSpread!=="")+Number(minNet!=="")+Number(readiness!=="all");$("filterCount").textContent=filterCount;$("filterCount").classList.toggle("hidden",!filterCount);$("exchangeCount").textContent=exchanges.size;
  const rowsHTML=rows.map(row=>{
    const s=status(row), long=row.legs.long, short=row.legs.short;
    const venue=(leg,side)=>`<div class="leg-line"><span class="direction ${side}">${side==="long"?"↑":"↓"}</span>${mark(leg.exchange)}<span class="exchange-name">${esc(names[leg.exchange]||leg.exchange)}</span><span class="price">${price(side==="long"?leg.ask:leg.bid)}</span></div>`;
    const fund=leg=>`<div class="fund-line"><span class="rate">${pct(leg.funding_rate_pct,4)}</span><span class="period">${period(leg.funding_interval_ms)}</span><span class="countdown" title="Время до ближайшего начисления${data.mode!=="live"?" на момент снимка":""}">◷ ${countdown(leg.funding_timestamp_ms)}</span></div>`;
    return `<tr data-row-key="${esc($("mode").value==="best"?"asset:"+row.asset:"pair:"+row.id)}" data-open="${esc(row.id)}" tabindex="0" class="${selected===row.id?"selected ":""}${isFresh(row)?"":"stale-row"}" aria-label="${esc(row.symbol)} ${esc(names[long.exchange])} LONG, ${esc(names[short.exchange])} SHORT"><td class="star-cell"><button class="star ${favorites.has(row.id)?"active":""}" data-star="${esc(row.id)}" aria-label="${favorites.has(row.id)?"Убрать из избранного":"В избранное"} ${esc(row.symbol)}" aria-pressed="${favorites.has(row.id)}">${favorites.has(row.id)?"★":"☆"}</button></td><td><span class="coin">${esc(row.symbol)}</span><div class="asset-sub">${esc(row.asset)}${$("mode").value==="best"?` <span title="Другие направления в текущем фильтре">+${(counts.get(row.asset)||1)-1}</span>`:""}</div></td><td>${venue(long,"long")}${venue(short,"short")}</td><td>${fund(long)}${fund(short)}</td><td class="col-history"><div class="history-rate">${pct(long.funding_history_24h_pct)}</div><div class="history-rate">${pct(short.funding_history_24h_pct)}</div></td><td class="numeric col-fundnet">${badge(row.funding_net_24h_pct)}</td><td class="numeric">${badge(row.open_spread_pct)}</td><td class="numeric col-net">${badge(row.economics?.convergence_net_scenario_usdt,money)}<div class="net-note">${row.economics?.hold_hours!=null?`${num(row.economics.hold_hours,0)}ч · USDT`:'не рассчитан'}</div></td><td><div class="status-label ${s.tone}"><span class="status-dot"></span>${s.label}</div><div class="status-sub" title="${esc(s.sub)}">${esc(s.sub)}</div></td></tr>`;
  }).join("");
  if(rowsHTML!==renderedRows){ScannerTable.update($("rows"),rowsHTML);renderedRows=rowsHTML;}
  $("empty").classList.toggle("hidden",rows.length>0);
  $("emptyTitle").textContent=data.rows.length?"Нет совпадений":"Пока нет наблюдений";
  $("emptyText").textContent=data.rows.length?"Попробуй изменить биржи, поиск или условия фильтра.":"Запусти монитор и дождись загрузки котировок. Если есть сохранённая история, здесь появится её последний снимок.";
  $("emptyReset").classList.toggle("hidden",!data.rows.length);
  document.querySelectorAll("[data-sort]").forEach(button=>button.querySelector(".sort-icon").textContent=button.dataset.sort===sort?(order===-1?"↓":"↑"):"");
}
function renderHeader(){
  if(!data){if(!connected){$("connection").textContent="Нет связи";$("connection").className="connection offline";$("dataBanner").className="data-banner offline";$("dataBanner").textContent="Не удалось получить данные локального сервера. Проверь, запущен ли он, и нажми обновление.";}return;}
  const connection=$("connection"),banner=$("dataBanner"),age=ago(now()-data.generated_at_ms);
  connection.className="connection";banner.className="data-banner";
  if(!connected){connection.textContent="Нет связи";connection.classList.add("offline");banner.classList.add("offline");banner.textContent="Связь с локальным сервером потеряна. Показаны последние полученные значения; они не являются действующим сигналом.";}
  else if(data.mode==="live"){connection.textContent="Монитор онлайн";connection.classList.add("live");banner.classList.add("live");banner.textContent="Сырой спред не включает расходы. Условный net учитывает модель комиссий и funding; исполнение сделок не проверяется.";}
  else if(data.mode==="archive"||data.mode==="stopped"){connection.textContent="Архивный снимок";banner.textContent=`${data.active_state==="stopped"?"Монитор остановлен. ":""}Показан сохранённый снимок от ${date(data.generated_at_ms)}. Цены, ставки и статусы относятся к этому моменту и сейчас не действуют.`;}
  else if(data.mode==="stale"){connection.textContent="Нет свежего снимка";banner.textContent="Снимок устарел. Проверяй состояние монитора; подтверждения здесь не являются действующими.";}
  else{connection.textContent="Ожидание данных";banner.textContent="Нет сохранённого снимка. Монитор загружает котировки или ещё не запущен.";}
  if(data.error){banner.className="data-banner offline";banner.textContent=data.error+". "+banner.textContent;}
  $("snapshotTime").textContent=data.generated_at_ms?`${date(data.generated_at_ms)} · ${age} назад`:"Ожидание снимка";
  $("exchangeHealth").innerHTML=data.exchanges.map(x=>`<span class="health-item" title="${esc(x.last_error||x.metadata_error||x.state)}"><span class="health-dot ${data.mode==="live"&&connected?(x.state==="streaming"?(x.usable_quotes>0?"live":""):"bad"):""}"></span>${esc(names[x.exchange]||x.exchange)} <span>${data.mode==="live"?`${x.usable_quotes}/${x.markets}`:"архив"}</span></span>`).join("");
}
async function get(url){const response=await fetch(url,{signal:AbortSignal.timeout(6000),cache:"no-store"});if(!response.ok)throw new Error(String(response.status));return response.json();}
async function refresh(){if(inflight)return;inflight=true;try{data=await get("/api/state");offset=data.server_time_ms-Date.now();connected=true;renderHeader();renderTable();if(selected){const current=data.rows.find(x=>x.id===selected);if(current&&detailData){detailData.item=current;selectedMissing=false;renderDetail();}if(Date.now()-detailTimer>9000)loadDetail();}}catch{connected=false;renderHeader();renderTable();if(detailData){detailData.item.confirmed_current=false;renderDetail();}}finally{inflight=false;}}
function closeDetail(){setChartExpanded(false);selected=null;detailRequest++;$("detail").classList.add("hidden");$("backdrop").classList.add("hidden");document.body.style.overflow="";renderTable();}
async function openDetail(id,preserve=false){selected=id;detailData=null;detailLoading=true;selectedMissing=false;chartEnd=null;if(!preserve)tab="spread";fundingLimit=50;hovered=null;$("zoom").value=100;$("detail").classList.remove("hidden");$("backdrop").classList.remove("hidden");$("detail").scrollTop=0;$("detail").focus();document.body.style.overflow="hidden";$("detailTitle").textContent=data.rows.find(x=>x.id===id)?.symbol||"Связка";$("detailDirection").textContent=(()=>{const row=data.rows.find(x=>x.id===id);return row?`${names[row.legs.long.exchange]} LONG → ${names[row.legs.short.exchange]} SHORT`:"Направление недоступно";})();$("fundingSubLabel").textContent="Загрузка наблюдений funding…";$("moreFunding").classList.add("hidden");$("detailNotice").textContent="Загрузка сохранённых наблюдений…";for(const id of ["legs","pairMetrics","economicContent","events","chartLegend","fundingHead","fundingRows","fundingSummary"])$(id).innerHTML="";for(const id of ["mainChart","fundingChart"]){const canvas=$(id);canvas.getContext('2d').clearRect(0,0,canvas.width,canvas.height);canvas._domain=null;}$("chartInfo").textContent="Загрузка истории…";$("chartReadout").textContent="";renderTable();renderLegControls();await loadDetail();}
async function loadDetail(){if(!selected)return;const id=selected,request=++detailRequest;detailTimer=Date.now();detailLoading=true;$("chartInfo").textContent="Обновление истории…";renderLegControls();renderFundingHistory();try{const result=await get(`/api/pair?id=${encodeURIComponent(id)}&minutes=${$("chartWindow").value}&days=${$("fundingDays").value}${chartEnd==null?"":"&end_ms="+chartEnd}`);if(request!==detailRequest||selected!==id)return;detailData=result;selectedMissing=false;detailLoading=false;renderDetail();}catch{if(request!==detailRequest||selected!==id)return;detailLoading=false;selectedMissing=true;renderLegControls();if(detailData){detailData.item.confirmed_current=false;detailData.item.data_fresh=false;detailData.item.economic_fresh=false;renderDetail();}else{$("detailNotice").textContent="Связка недоступна в текущем снимке. Она могла выйти из отбора или история временно занята.";$("chartInfo").textContent="История недоступна";}}}
function renderLegControls(){
  const current=data?.rows.find(x=>x.id===selected)||detailData?.item;
  for(const side of ['long','short']){
    const control=$(side+'Venue'),other=side==='long'?'short':'long';
    const options=current?(data?.rows||[]).filter(x=>x.asset===current.asset&&x.legs[other].exchange===current.legs[other].exchange):[];
    const html=options.map(row=>`<option value="${esc(row.id)}">${esc(names[row.legs[side].exchange]||row.legs[side].exchange)}</option>`).join('');
    if(control.innerHTML!==html)control.innerHTML=html;
    control.value=selected;control.disabled=!connected||detailLoading||!options.length;
  }
  const reverse=current&&(data?.rows||[]).find(x=>x.asset===current.asset&&x.legs.long.exchange===current.legs.short.exchange&&x.legs.short.exchange===current.legs.long.exchange);
  $('swapLegs').disabled=!connected||detailLoading||!reverse;
  $('swapLegs').dataset.target=reverse?.id||'';
}
function renderFundingHistory(){
  document.querySelectorAll('[data-funding-view]').forEach(x=>x.setAttribute('aria-selected',String(x.dataset.fundingView===fundingView)));
  const result=detailData?.funding_comparison;
  $('moreFunding').classList.add('hidden');
  if(detailLoading){$('fundingSummary').textContent='Загрузка истории выбранных бирж…';$('fundingRows').innerHTML='';return;}
  if(result&&result.days!==Number($('fundingDays').value)){$('fundingSummary').textContent='История выбранного периода недоступна. Повтори выбор периода.';$('fundingRows').innerHTML='';return;}
  if(!result){$('fundingSummary').textContent='Архив исторических начислений ещё не загружен.';$('fundingRows').innerHTML='';return;}
  const coverage=['long','short'].map(side=>`${side.toUpperCase()} ${names[detailData.item.legs[side].exchange]||detailData.item.legs[side].exchange}: ${result.coverage[side].events} записей · ${result.coverage[side].covered?'покрытие проверено':'покрытие неполное'}`).join(' / ');
  $('fundingSummary').textContent=`${date(result.start_exclusive_ms)} — ${date(result.end_inclusive_ms)}. ${coverage}. Суммы записей: LONG ${pct(result.totals_pct.long)} / SHORT ${pct(result.totals_pct.short)}. Разница за полный период: ${pct(result.covered_net_pct)}.${result.truncated?' Лимит 1000 записей на ногу; показана последняя часть истории.':''}`;
  const comparison=fundingView==='compare';
  const labels=comparison?['Время · Киев','LONG · ставка','SHORT · ставка','Σ LONG','Σ SHORT','Σ S − L']:['Время · Киев',fundingView.toUpperCase()+' · ставка','Сумма записей'];
  const head='<tr>'+labels.map(x=>`<th>${x}</th>`).join('')+'</tr>';
  if($('fundingHead').innerHTML!==head)$('fundingHead').innerHTML=head;
  const rows=[...result.rows].reverse().filter(row=>comparison||row[fundingView+'_rate_pct']!=null);
  const html=rows.slice(0,fundingLimit).map(row=>{const values=comparison?[row.long_rate_pct,row.short_rate_pct,row.long_sum_pct,row.short_sum_pct,row.observed_net_pct]:[row[fundingView+'_rate_pct'],row[fundingView+'_sum_pct']];return `<tr><td>${date(row.time)}</td>${values.map(value=>`<td class="${signClass(value)}">${pct(value,4)}</td>`).join('')}</tr>`;}).join('')||`<tr><td colspan="${labels.length}">Нет сохранённых начислений за этот период.</td></tr>`;
  if($('fundingRows').innerHTML!==html)$('fundingRows').innerHTML=html;
  $('moreFunding').classList.toggle('hidden',rows.length<=fundingLimit);
}
function renderDetail(){
  if(!detailData)return;renderLegControls();renderFundingHistory();const row=detailData.item,s=status(row),e=row.economics;
  $("detailTitle").textContent=row.symbol;$("detailDirection").textContent=`${names[row.legs.long.exchange]} LONG → ${names[row.legs.short.exchange]} SHORT`;
  $("detailFavorite").textContent=favorites.has(row.id)?"★":"☆";
  const notice=$("detailNotice");notice.className="detail-notice";
  if(selectedMissing)notice.textContent="Связка больше не доступна в текущем снимке. Ниже — последние полученные наблюдения.";
  else if(!connected)notice.textContent="Нет связи с локальным сервером. Текущая карточка не подтверждает действующий сигнал.";
  else if(data.mode!=="live")notice.textContent=`Архив от ${date(detailData.generated_at_ms)}. Все значения относятся к моменту сохранения.`;
  else if(!isFresh(row))notice.textContent="Котировки устарели. Дождись независимого обновления обеих ног.";
  else{notice.classList.add("live");notice.textContent=`${s.label}. Доступность торговли не проверена; расчёт основан на публичных данных.`;}
  $("pairMetrics").innerHTML=[['Спред входа · сырой',row.open_spread_pct],['Спред выхода · сейчас',row.exit_spread_pct],['F net 24ч · история',row.funding_net_24h_pct]].map(([label,v])=>`<div class="metric"><div class="metric-label">${label}</div><div class="metric-value ${signClass(v)}">${pct(v)}</div></div>`).join("");
  $("legs").innerHTML=Object.entries(row.legs).map(([side,leg])=>{const volume=leg.volume_24h_usdt;const volumeText=volume==null?"—":volume>=1e6?"$"+num(volume/1e6,2)+"M":"$"+num(volume,0);return `<article class="leg-card"><div class="leg-title"><span class="direction ${side}">${side==="long"?"↑":"↓"}</span>${mark(leg.exchange)}${esc(names[leg.exchange])}<span class="side-text ${side==="long"?"positive":"negative-text"}">${side.toUpperCase()}</span></div><dl><dt>Ask</dt><dd>${price(leg.ask)}</dd><dt>Bid</dt><dd>${price(leg.bid)}</dd><dt>Funding · ${period(leg.funding_interval_ms)}</dt><dd>${pct(leg.funding_rate_pct,4)}</dd><dt>Следующее начисление</dt><dd>${countdown(leg.funding_timestamp_ms)}</dd><dt>Ставки за 24ч</dt><dd>${pct(leg.funding_history_24h_pct)}</dd><dt>Оборот за 24ч</dt><dd>${volumeText}</dd><dt>Котировка</dt><dd>${date(leg.source_time_ms,true)}</dd></dl><div class="sub">${esc(leg.native_id)} · ${data.mode==="live"&&isFresh(row)?"источник свежий":"значения снимка"}</div></article>`;}).join("");
  if(!e){$("economicContent").innerHTML='<div class="reason-box">Детальный расчёт этой связки ещё не выполнен. Монитор проверяет глубину ограниченного набора кандидатов; сырой спред не подтверждает исполнение.</div>';}
  else{
    const fields=[['Базовое количество',price(e.base_quantity)],['Контракты L / S',`${price(e.long_contracts)} / ${price(e.short_contracts)}`],['Номинал обеих ног',money(e.entry_notional_usdt)],['Бюджет с входными расходами',money(e.entry_budget_used_usdt)],['Четыре комиссии, оценка',money(e.four_fee_convergence_estimate_usdt)],['Funding, сценарий',money(e.funding_scenario_usdt)],['LONG · VWAP входа',price(e.fills?.long_entry?.price)],['SHORT · VWAP входа',price(e.fills?.short_entry?.price)],['Закрыть сейчас, до funding',money(e.close_now_net_before_funding_usdt)],['Горизонт funding',e.hold_hours!=null?num(e.hold_hours,0)+'ч':'—']];
    $("economicContent").innerHTML=(e.reasons?.length?`<div class="reason-box">${e.reasons.map(x=>esc(reason(x))).join('<br>')}</div>`:"")+`<div class="economic-grid">${fields.map(([label,v])=>`<div class="economic-field"><span>${label}</span><b>${v}</b></div>`).join("")}</div><div class="scenario-result"><span>Условный net при схождении</span><strong class="${signClass(e.convergence_net_scenario_usdt)}">${money(e.convergence_net_scenario_usdt)} <small>USDT</small></strong></div><div class="model-notes">Выход предполагается по одной будущей цене. Funding повторяет текущую ставку и интервал. Все комиссии — оценка, тариф аккаунта не подтверждён.${row.signal?` Серия в снимке: ${row.signal.confirmations} наблюдений.`:""} Фактический PnL отсутствует.</div>`;
  }
  $("events").innerHTML=detailData.events?.length?[...detailData.events].reverse().slice(0,12).map(event=>`<div class="event-row"><span class="event-time" title="${date(event.timestamp_ms)}">${date(event.timestamp_ms,true)}</span><div class="event-text">${esc(eventNames[event.kind]||event.kind)}<div class="event-reason">${event.reasons.map(x=>esc(reason(x))).join(' · ')||esc(event.to_state)}</div></div></div>`).join(""):'<div class="no-data">Для этого направления пока нет сохранённых событий.</div>';
  document.querySelectorAll('[data-tab]').forEach(x=>x.setAttribute('aria-selected',String(x.dataset.tab===tab)));renderCharts();
}

function chart(canvas, series, domain, unit, hover){
  const rect=canvas.getBoundingClientRect(),dpr=window.devicePixelRatio||1,w=Math.max(250,rect.width),h=rect.height||150;
  canvas.width=Math.round(w*dpr);canvas.height=Math.round(h*dpr);const ctx=canvas.getContext('2d');ctx.scale(dpr,dpr);ctx.clearRect(0,0,w,h);
  const left=13,right=63,top=12,bottom=25,pw=w-left-right,ph=h-top-bottom;
  canvas._domain=domain;canvas._plot=[left,w-right];
  const values=series.flatMap(s=>s.rows.filter(p=>p.time>=domain[0]&&p.time<=domain[1]).flatMap(x=>chartPrefs.extremes?[x.value,x.min,x.max]:[x.value]).filter(x=>x!=null&&Number.isFinite(x)));
  if(!values.length){ctx.fillStyle='#7e95a6';ctx.font='11px Segoe UI';ctx.textAlign='center';ctx.fillText('Нет сохранённых наблюдений в этом диапазоне',w/2,h/2);return;}
  let min=Math.min(...values),max=Math.max(...values),padding=(max-min)*.16||Math.max(Math.abs(max)*.02,.005);min-=padding;max+=padding;
  const begin=domain[0],end=Math.max(domain[1],begin+1),x=t=>left+(t-begin)/(end-begin)*pw,y=v=>top+(max-v)/(max-min)*ph;
  ctx.font='10px Consolas';ctx.lineWidth=.7;
  for(let i=0;i<5;i++){const v=min+(max-min)*i/4,py=y(v);ctx.strokeStyle='#3c4851';ctx.beginPath();ctx.moveTo(left,py);ctx.lineTo(w-right,py);if(chartPrefs.grid)ctx.stroke();ctx.fillStyle='#899caa';ctx.textAlign='left';ctx.fillText(num(v,unit==='price'?(Math.max(Math.abs(min),Math.abs(max))<1?6:Math.max(Math.abs(min),Math.abs(max))<100?4:2):3),w-right+8,py+3);}
  for(let i=0;i<4;i++){const t=begin+(end-begin)*i/3,px=x(t);ctx.strokeStyle='#35424c';ctx.beginPath();ctx.moveTo(px,top);ctx.lineTo(px,h-bottom);if(chartPrefs.grid)ctx.stroke();ctx.fillStyle='#8196a5';ctx.textAlign=i===0?'left':i===3?'right':'center';const label=tab==='funding'?new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Kyiv',day:'2-digit',month:'2-digit'}).format(new Date(t)):new Intl.DateTimeFormat('ru-RU',{timeZone:'Europe/Kyiv',hour:'2-digit',minute:'2-digit'}).format(new Date(t));ctx.fillText(label,px,h-7);}
  if(min<0&&max>0){ctx.strokeStyle='#70808c';ctx.setLineDash([3,4]);ctx.beginPath();ctx.moveTo(left,y(0));ctx.lineTo(w-right,y(0));ctx.stroke();ctx.setLineDash([]);}
  for(const s of series){ctx.strokeStyle=s.color;ctx.fillStyle=s.color;ctx.lineWidth=1.25;let prev=null;ctx.beginPath();for(const p of s.rows){if(p.value==null||!Number.isFinite(p.value)){prev=null;continue;}if(p.time<begin||p.time>end)continue;if(!prev||p.time-prev.time>(s.maxGap||10000)||(p.interval_ms!=null&&prev.interval_ms!=null&&p.interval_ms!==prev.interval_ms)||s.events)ctx.moveTo(x(p.time),y(p.value));else ctx.lineTo(x(p.time),y(p.value));if(s.events||s.minute){ctx.fillRect(x(p.time)-1.5,y(p.value)-1.5,3,3);}prev=p;}ctx.stroke();if(s.minute&&chartPrefs.extremes){ctx.globalAlpha=.4;ctx.beginPath();for(const p of s.rows){if(p.min!=null&&p.max!=null&&p.time>=begin&&p.time<=end){ctx.moveTo(x(p.time),y(p.min));ctx.lineTo(x(p.time),y(p.max));}}ctx.stroke();ctx.globalAlpha=1;}if(s.rows.length===1&&s.rows[0].time>=begin&&s.rows[0].time<=end&&s.rows[0].value!=null){const p=s.rows[0];ctx.fillRect(x(p.time)-2,y(p.value)-2,4,4);}}
  if(hover!=null){ctx.strokeStyle='#a5b4c3';ctx.setLineDash([3,3]);ctx.beginPath();ctx.moveTo(x(hover),top);ctx.lineTo(x(hover),h-bottom);ctx.stroke();ctx.setLineDash([]);}
  canvas._domain=[begin,end];canvas._plot=[left,w-right];
}
function activeChartRange(){return tab==='funding'?detailData?.funding_range:detailData?.chart_range;}
function applyChartSettings(save=false){
  $('mainChart').style.height=chartPrefs.height+'px';$('fundingChart').style.height=chartPrefs.fundingHeight+'px';
  $('chartHeight').value=chartPrefs.height;$('fundingHeight').value=chartPrefs.fundingHeight;
  for(const [id,key] of [['chartExtremes','extremes'],['chartGrid','grid'],['chartSubplot','subplot']])$(id).checked=chartPrefs[key];
  if(save)try{localStorage.setItem('arbitrage.charts',JSON.stringify(chartPrefs));}catch{}
  renderCharts();
}
function setChartExpanded(value){
  chartExpanded=value;$('chartSection').classList.toggle('chart-expanded',value);
  $('chartExpand').setAttribute('aria-expanded',String(value));$('chartExpand').setAttribute('aria-label',value?'Свернуть графики':'Развернуть графики');
  if(value)$('chartSection').focus();renderCharts();
}
function navigateChart(direction){
  if(!detailData||detailLoading)return;const range=activeChartRange();if(!range)return;
  const next=ChartModel.navigate(range,$('zoom').value,direction);if(next===undefined)return;
  chartEnd=next==null?null:Math.round(next);hovered=null;$('chartReadout').textContent='';loadDetail();
}
function latestChart(){chartEnd=null;hovered=null;$('zoom').value=100;$('chartReadout').textContent='';loadDetail();}
function chartReadout(){
  if(hovered==null||!$('mainChart')._domain)return;
  const bounds=$('mainChart')._domain,tolerance=tab==='funding'?6*3600000:detailData.history_source==='minute'?90000:10000;
  const values=chartSeries.map(series=>{const point=ChartModel.nearest(series.rows,hovered,bounds,tolerance);return point?`${series.label}: ${tab==='price'?price(point.value):pct(point.value,4)} (${date(point.time)})`:series.label+': нет записи рядом';});
  $('chartReadout').textContent=values.join(' · ');
}
function renderCharts(){
  if(!detailData)return;const range=activeChartRange();if(!range)return;
  $('chartWindow').classList.toggle('hidden',tab==='funding');
  const domain=ChartModel.domain(range,$('zoom').value),points=detailData.points||[],green='#38c28b',red='#ed6d80';
  const visible=points.filter(p=>p.time>=domain[0]&&p.time<=domain[1]);
  if(tab==='funding')chartSeries=detailData.funding_series.map(s=>({label:s.side.toUpperCase(),color:s.side==='long'?green:red,events:true,rows:s.rows.map(p=>({time:p.time,value:p.rate_pct}))}));
  else chartSeries=['long','short'].map((side,i)=>{const field=tab==='spread'?(i===0?'entry':'exit'):(i===0?'long_price':'short_price');return {label:tab==='spread'?(i===0?'Вход':'Выход'):(i===0?'LONG ask':'SHORT bid'),color:i===0?green:red,minute:detailData.history_source==='minute',maxGap:detailData.history_source==='minute'?90000:10000,rows:visible.map(p=>({time:p.time,value:p[field],min:p.ranges?.[field]?.min,max:p.ranges?.[field]?.max}))};});
  $('chartLegend').innerHTML=chartSeries.map((s,i)=>`<span><i class="legend-key ${i?'red':''}"></i>${esc(s.label)}</span>`).join('');
  $('chartContext').textContent=`${detailData.item.symbol} · ${names[detailData.item.legs.long.exchange]} LONG → ${names[detailData.item.legs.short.exchange]} SHORT`;
  chart($('mainChart'),chartSeries,domain,tab==='price'?'price':'percent',hovered);
  const showFunding=tab!=='funding'&&chartPrefs.subplot;
  $('fundingChart').classList.toggle('hidden',!showFunding);$('fundingSubLabel').classList.toggle('hidden',!showFunding);
  $('fundingSubLabel').textContent=`Funding, % за свой период · LONG ${period(detailData.item.legs.long.funding_interval_ms)} / SHORT ${period(detailData.item.legs.short.funding_interval_ms)}`;
  if(showFunding)chart($('fundingChart'),['long','short'].map(side=>({color:side==='long'?green:red,minute:detailData.history_source==='minute',maxGap:detailData.history_source==='minute'?90000:10000,rows:detailData.observed_funding?.[side]||[]})),domain,'percent',hovered);
  $('chartInfo').textContent=detailLoading?'Обновление истории…':tab==='funding'?`Сохранённые события funding, не выплаты аккаунта${detailData.funding_series.some(s=>s.truncated)?' · последние 1000 событий на ногу':''}`:`${visible.length} ${detailData.history_source==='minute'?'минутных точек':'наблюдений'}${detailData.truncated?' · последние 300 снимков':''}${Object.values(detailData.history_pending||{}).some(x=>x>0)?' · сводки ещё догружаются':''} · без заполнения пропусков`;
  $('chartRange').textContent=`Окно: ${date(domain[0])} — ${date(domain[1])}. Время: Киев.`;
  $('chartFollow').textContent=chartEnd==null?(data.mode==='live'?'Следим за обновлениями':'Конец архива'):'Просмотр истории';
  $('chartBack').disabled=detailLoading||ChartModel.navigate(range,$('zoom').value,-1)===undefined;
  $('chartForward').disabled=detailLoading||range.end_ms>=range.latest_ms;
  chartReadout();
}

$("exchangeChoices").innerHTML=Object.entries(names).map(([id,name])=>`<label><input type="checkbox" data-exchange-choice="${id}" checked>${mark(id)}${name}</label>`).join('');
function placePopover(details){const body=details.querySelector('.popover-body');if(!body)return;if(window.innerWidth<=620&&details.open){const anchor=details.querySelector('summary').getBoundingClientRect();body.style.position='fixed';body.style.width=Math.min(280,window.innerWidth-24)+'px';body.style.right='auto';body.style.left=Math.max(12,Math.min(anchor.left,window.innerWidth-body.offsetWidth-12))+'px';body.style.top=Math.max(12,Math.min(anchor.bottom+6,window.innerHeight-body.offsetHeight-12))+'px';body.style.maxHeight=(window.innerHeight-24)+'px';body.style.overflowY='auto';}else{for(const property of ['position','width','right','left','top','max-height','overflow-y'])body.style.removeProperty(property);}}
document.querySelectorAll('.popover').forEach(details=>details.addEventListener('toggle',()=>placePopover(details)));
document.addEventListener('click',event=>{document.querySelectorAll('.popover[open]').forEach(details=>{if(!details.contains(event.target))details.open=false;});});
$("mode").value=prefs.mode||'best';
const initialHidden=prefs.hidden??(window.innerWidth<1100?['history','fundnet','net']:[]);
document.querySelectorAll('[data-column]').forEach(x=>{x.checked=!initialHidden.includes(x.dataset.column);document.body.classList.toggle('hide-'+x.dataset.column,!x.checked);x.addEventListener('change',()=>{document.body.classList.toggle('hide-'+x.dataset.column,!x.checked);savePrefs();});});
for(const id of ['search','minSpread','minNet'])$(id).addEventListener('input',renderTable);
$("readiness").addEventListener('change',renderTable);$("mode").addEventListener('change',()=>{renderTable();savePrefs();});
document.querySelectorAll('[data-exchange-choice]').forEach(x=>x.addEventListener('change',()=>{x.checked?exchanges.add(x.dataset.exchangeChoice):exchanges.delete(x.dataset.exchangeChoice);renderTable();}));
document.querySelectorAll('[data-sort]').forEach(button=>button.addEventListener('click',()=>{order=sort===button.dataset.sort?-order:-1;sort=button.dataset.sort;renderTable();savePrefs();}));
$("favoriteOnly").addEventListener('click',()=>{favoriteOnly=!favoriteOnly;$("favoriteOnly").setAttribute('aria-pressed',String(favoriteOnly));renderTable();});
$("clearFilters").addEventListener('click',resetFilters);$("emptyReset").addEventListener('click',resetFilters);
$("rows").addEventListener('click',event=>{const star=event.target.closest('[data-star]');if(star){toggleFavorite(star.dataset.star);return;}const row=event.target.closest('[data-open]');if(row)openDetail(row.dataset.open);});
$("rows").addEventListener('keydown',event=>{if(event.key==='Enter'&&event.target.matches('tr[data-open]'))openDetail(event.target.dataset.open);});
$("closeDetail").addEventListener('click',closeDetail);$("backdrop").addEventListener('click',closeDetail);$("detailFavorite").addEventListener('click',()=>selected&&toggleFavorite(selected));
document.querySelectorAll('[data-tab]').forEach(x=>x.addEventListener('click',()=>{tab=x.dataset.tab;hovered=null;$('chartReadout').textContent='';$('zoom').value=100;if(chartEnd!=null){chartEnd=null;loadDetail();}else renderDetail();}));
for(const side of ['long','short'])$(side+'Venue').addEventListener('change',event=>{if(event.target.value&&event.target.value!==selected)openDetail(event.target.value,true);});
$('swapLegs').addEventListener('click',()=>{const id=$('swapLegs').dataset.target;if(id)openDetail(id,true);});
$('fundingDays').addEventListener('change',()=>{fundingLimit=50;loadDetail();renderFundingHistory();});
document.querySelectorAll('[data-funding-view]').forEach(x=>x.addEventListener('click',()=>{fundingView=x.dataset.fundingView;fundingLimit=50;renderFundingHistory();}));
$('moreFunding').addEventListener('click',()=>{fundingLimit+=50;renderFundingHistory();});
$("chartWindow").addEventListener('change',()=>{chartEnd=null;hovered=null;$('chartReadout').textContent='';$("zoom").value=100;loadDetail();});
$("zoom").addEventListener('input',renderCharts);$("resetZoom").addEventListener('click',()=>{$("zoom").value=100;hovered=null;$("chartReadout").textContent="";renderCharts();});
$('mainChart').addEventListener('pointermove',event=>{const canvas=event.currentTarget;if(detailLoading||!canvas._domain)return;const x=event.clientX-canvas.getBoundingClientRect().left,[left,right]=canvas._plot;hovered=canvas._domain[0]+Math.max(0,Math.min(1,(x-left)/(right-left)))*(canvas._domain[1]-canvas._domain[0]);renderCharts();});
$("mainChart").addEventListener('pointerleave',()=>{hovered=null;renderCharts();});
$("mainChart").addEventListener('wheel',event=>{event.preventDefault();$("zoom").value=Math.max(20,Math.min(100,Number($("zoom").value)+(event.deltaY>0?10:-10)));renderCharts();},{passive:false});
$("refresh").addEventListener('click',()=>{detailTimer=0;refresh();});
document.addEventListener('keydown',event=>{if(PaperPanel.isOpen())return;if(alertsOpen){if(event.key==='Escape'){event.preventDefault();closeAlerts();return;}if(event.key==='Tab'){const controls=[...$('alertsPanel').querySelectorAll('button,input')].filter(x=>!x.disabled&&x.offsetParent!==null);if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0]?.focus();}}return;}if(event.key==='Escape'){if($('chartSettings').open){$('chartSettings').open=false;return;}if(chartExpanded){setChartExpanded(false);$('chartExpand').focus();return;}document.querySelectorAll('.popover[open]').forEach(x=>x.open=false);closeDetail();}if(event.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)){event.preventDefault();$("search").focus();}if(event.key==='Tab'&&selected){const controls=[...(chartExpanded?$('chartSection'):$("detail")).querySelectorAll('button,select,input,summary')].filter(x=>!x.disabled&&x.offsetParent!==null);if(chartExpanded&&document.activeElement===$('chartSection')){event.preventDefault();(event.shiftKey?controls.at(-1):controls[0])?.focus();}else if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1).focus();}else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0].focus();}}});
window.addEventListener('resize',()=>{renderCharts();document.querySelectorAll('.popover[open]').forEach(placePopover);});
$('chartBack').addEventListener('click',()=>navigateChart(-1));$('chartForward').addEventListener('click',()=>navigateChart(1));$('chartLatest').addEventListener('click',latestChart);
$('chartExpand').addEventListener('click',()=>setChartExpanded(!chartExpanded));
for(const [id,key] of [['chartHeight','height'],['fundingHeight','fundingHeight'],['chartExtremes','extremes'],['chartGrid','grid'],['chartSubplot','subplot']])$(id).addEventListener('change',()=>{chartPrefs[key]=$(id).type==='checkbox'?$(id).checked:Number($(id).value);applyChartSettings(true);});
$('resetChartSettings').addEventListener('click',()=>{chartPrefs={...chartDefaults};applyChartSettings(true);});
$('chartSection').addEventListener('keydown',event=>{if(['INPUT','SELECT'].includes(event.target.tagName))return;if(event.key==='ArrowLeft'){event.preventDefault();navigateChart(-1);}if(event.key==='ArrowRight'){event.preventDefault();navigateChart(1);}if(event.key.toLowerCase()==='e'){event.preventDefault();latestChart();}if(event.key.toLowerCase()==='f'){event.preventDefault();setChartExpanded(!chartExpanded);}});
$('openAlerts').addEventListener('click',openAlerts);$('closeAlerts').addEventListener('click',closeAlerts);$('alertsBackdrop').addEventListener('click',closeAlerts);
$('alertsRefresh').addEventListener('click',()=>refreshAlerts());$('onlyUnreadAlerts').addEventListener('change',renderAlerts);$('olderAlerts').addEventListener('click',()=>refreshAlerts(true));
$('markAlertsRead').addEventListener('click',()=>{alerts.read=alerts.latest;alerts.unread=0;try{localStorage.setItem(alertReadKey(alerts.scope),JSON.stringify(alerts.read));alerts.readError=false;}catch{alerts.readError=true;}renderAlerts();});
$('alertsList').addEventListener('click',event=>{const button=event.target.closest('[data-alert-pair]');if(button&&data?.rows.some(row=>row.id===button.dataset.alertPair)){closeAlerts();openDetail(button.dataset.alertPair);}});
window.addEventListener('storage',event=>{if(alerts.scope&&event.key===alertReadKey(alerts.scope)){alerts.read=savedAlertRead(alerts.scope);refreshAlerts();}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden)refreshAlerts();});
applyChartSettings();refresh();refreshAlerts();setInterval(()=>{if(!document.hidden)refreshAlerts();},5000);setInterval(refresh,2000);setInterval(()=>{renderHeader();renderTable();if(detailData)renderDetail();},1000);

PaperPanel.init({get,esc,money:(value,signed=false)=>num(value,2,signed),date,venue:id=>names[id]||id,update:ScannerTable.update,canOpen:id=>!!data?.rows.some(row=>row.id===id),openPair:openDetail,onOpen:()=>{if(alertsOpen)closeAlerts();if(selected)closeDetail();}});
