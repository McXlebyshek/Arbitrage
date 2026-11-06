(function(root){
  'use strict';
  function domain(range,zoom){
    const end=range.end_ms,span=Math.max(1,end-range.start_ms)*Math.max(20,Math.min(100,Number(zoom)||100))/100;
    return [Math.max(0,end-span),end];
  }
  function navigate(range,zoom,direction){
    const [begin,end]=domain(range,zoom),step=Math.max(1,(end-begin)*.8);
    if(direction<0){
      if(range.earliest_ms==null||begin<=range.earliest_ms)return undefined;
      return Math.max(range.earliest_ms+1,end-step);
    }
    if(end>=range.latest_ms)return undefined;
    return end+step>=range.latest_ms?null:Math.round(end+step);
  }
  function nearest(rows,time,bounds,tolerance){
    let found=null;
    for(const row of rows){
      if(row.value==null||!Number.isFinite(row.value)||row.time<bounds[0]||row.time>bounds[1])continue;
      if(Math.abs(row.time-time)<=tolerance&&(!found||Math.abs(row.time-time)<Math.abs(found.time-time)))found=row;
    }
    return found;
  }
  const api={domain,navigate,nearest};
  if(typeof module==='object'&&module.exports)module.exports=api;else root.ChartModel=api;
})(typeof window==='undefined'?globalThis:window);
