/* Read-only paper journal. Kept separate from scanner updates. */
window.PaperPanel=(()=>{
  let api,opened=false,busy=false,ticket=0,feed=null,error=null,received=0,offset=0;
  const $=id=>document.getElementById(id);
  const signed=value=>api.money(value,true);
  const tone=value=>value===null||value===undefined?'':Number(value)>0?'positive':Number(value)<0?'negative':'';
  const metric=(label,value,note='')=>`<div class="paper-metric"><span>${label}</span><strong>${value}</strong>${note?`<small>${note}</small>`:''}</div>`;
  function render(){
    if(!api)return;
    $('paperStatus').textContent=error||(!feed?'Загрузка paper-журнала…':`${feed.mode==='replay'?'REPLAY · историческая симуляция':'Сохранённая симуляция'} · ${feed.total_filtered} позиций в фильтре${busy?' · обновление…':''}`);
    $('paperStatus').classList.toggle('paper-warning',!!error||feed?.mode==='replay');
    $('paperRefresh').disabled=busy;
    $('paperPrevious').disabled=busy||!feed||feed.offset===0;
    $('paperNext').disabled=busy||!feed?.has_next;
    $('paperPage').textContent=feed?.total_filtered?`${feed.offset+1}–${Math.min(feed.offset+feed.limit,feed.total_filtered)} из ${feed.total_filtered}`:'Нет записей';
    if(!feed)return;
    const s=feed.summary;
    const summary=metric('Открыто / закрыто',`${s.open_count} / ${s.closed_count}`)+metric('Зарезервировано',`${api.money(s.reserved_usdt)} USDT`)+
      metric('Закрытые · до funding',`${signed(s.closed_before_funding_usdt)} USDT`)+metric('Итог закрытых',s.closed_net_usdt===null?'Не рассчитан':`${signed(s.closed_net_usdt)} USDT`,`${s.covered_closed_count} из ${s.closed_count} с полным funding`);
    if($('paperSummary').innerHTML!==summary)$('paperSummary').innerHTML=summary;
    $('paperStats').textContent=`Среди ${s.covered_closed_count} закрытых с полным funding: в плюс ${s.covered_wins}, в минус ${s.covered_losses}, без изменения ${s.covered_flat}. Это результаты симуляции.`;
    const html=feed.rows.map(row=>{
      const v=row.valuation,closed=row.state==='closed',fresh=!error&&row.valuation_fresh&&feed.generated_at_ms+(Date.now()-received)<=row.fresh_until_ms;
      const direction=`${api.venue(row.pair.long_exchange)} LONG → ${api.venue(row.pair.short_exchange)} SHORT`;
      const funding=v.funding_status==='covered_paper_cashflows'?'Funding учтён по предоставленным ценам':v.funding_status==='rates_covered_marks_missing'?'Для funding не хватает mark prices':v.funding_status==='partial_history'?'Неполная история funding':'Funding ещё не сверен';
      return `<article class="paper-position" data-row-key="${api.esc(row.id)}"><div class="paper-position-top"><div><strong>${api.esc(row.pair.asset)}</strong><div class="paper-direction">${api.esc(direction)}</div></div><span class="paper-state ${!closed&&!fresh?'paper-warning':''}">${closed?'Закрыта':fresh?'Открыта · свежая оценка':'Открыта · оценка устарела'}</span></div><div class="paper-values"><div><span>До funding</span><b class="${tone(v.pnl_before_funding_usdt)}">${signed(v.pnl_before_funding_usdt)} USDT</b></div><div><span>Итог симуляции</span><b class="${tone(v.net_pnl_usdt)}">${v.net_pnl_usdt===null?'—':signed(v.net_pnl_usdt)+' USDT'}</b></div></div><div class="paper-funding">${funding}</div><div class="paper-details"><span>Количество: ${api.esc(row.base_quantity)}</span><span>Комиссии вход / выход: ${api.money(v.entry_fees_usdt)} / ${api.money(v.exit_fees_usdt)} USDT</span></div><div class="paper-times">Вход: ${api.date(row.opened_at_ms)}<br>${closed?'Закрытие':'Оценка'}: ${api.date(v.timestamp_ms)}</div>${api.canOpen(row.pair.id)?`<button class="secondary-button" data-paper-pair="${api.esc(row.pair.id)}">Открыть связку</button>`:'<span class="alert-unavailable">Связка отсутствует в текущей таблице</span>'}</article>`;
    }).join('');
    api.update($('paperRows'),html);
    $('paperEmpty').classList.toggle('hidden',feed.rows.length>0);
    $('paperEmpty').textContent=feed.source==='absent'?'Paper-журнал ещё не создан. Вход в виртуальную позицию доступен через CLI.':feed.source==='unsupported'?'Этот файл не содержит поддерживаемого paper-журнала.':'В выбранном фильтре позиций нет.';
  }
  async function refresh(force=false){
    if(busy&&!force)return;
    const current=++ticket;busy=true;render();
    try{
      const started=Date.now();
      const result=await api.get('/api/paper?'+new URLSearchParams({state:$('paperFilter').value,limit:'25',offset:String(offset)}));
      if(current!==ticket)return;
      if(result.classification!=='paper_dashboard')throw new Error('old_server');
      const pageChanged=!feed||feed.offset!==result.offset||feed.filter!==result.filter;
      feed=result;offset=result.offset;received=started;error=null;
      if(pageChanged)$('paperRows').scrollTop=0;
    }catch{if(current===ticket)error='Журнал недоступен. Сохранённые ниже данные могли устареть; обновите или перезапустите сервер UI.';}
    finally{if(current===ticket){busy=false;render();}}
  }
  function close(focus=true){if(!opened)return;opened=false;document.querySelector('.app').inert=false;$('paperPanel').classList.add('hidden');$('paperBackdrop').classList.add('hidden');$('openPaper').setAttribute('aria-expanded','false');document.body.style.overflow='';if(focus)$('openPaper').focus();}
  function open(){api.onOpen();opened=true;document.querySelector('.app').inert=true;$('paperPanel').classList.remove('hidden');$('paperBackdrop').classList.remove('hidden');$('openPaper').setAttribute('aria-expanded','true');document.body.style.overflow='hidden';$('closePaper').focus();refresh();}
  function init(options){
    api=options;$('openPaper').addEventListener('click',open);$('closePaper').addEventListener('click',()=>close());$('paperBackdrop').addEventListener('click',()=>close());
    $('paperRefresh').addEventListener('click',()=>refresh());$('paperFilter').addEventListener('change',()=>{offset=0;refresh(true);});
    $('paperPrevious').addEventListener('click',()=>{offset=Math.max(0,offset-25);refresh();});$('paperNext').addEventListener('click',()=>{offset+=25;refresh();});
    $('paperRows').addEventListener('click',event=>{const button=event.target.closest('[data-paper-pair]');if(button&&api.canOpen(button.dataset.paperPair)){close(false);api.openPair(button.dataset.paperPair);}});
    document.addEventListener('keydown',event=>{if(!opened)return;if(event.key==='Escape'){event.preventDefault();close();}if(event.key==='Tab'){const controls=[...$('paperPanel').querySelectorAll('button,select')].filter(x=>!x.disabled&&x.offsetParent!==null);if(!$('paperPanel').contains(document.activeElement)){event.preventDefault();(event.shiftKey?controls.at(-1):controls[0])?.focus();}else if(event.shiftKey&&document.activeElement===controls[0]){event.preventDefault();controls.at(-1)?.focus();}else if(!event.shiftKey&&document.activeElement===controls.at(-1)){event.preventDefault();controls[0]?.focus();}}});
    document.addEventListener('visibilitychange',()=>{if(opened&&!document.hidden)refresh();});
    setInterval(()=>{if(opened&&!document.hidden)refresh();},5000);setInterval(()=>{if(opened&&!document.hidden)render();},1000);
  }
  return {init,close,isOpen:()=>opened};
})();
