"""Exact closed-position statistics with bounded, extrema-preserving chart points."""
from decimal import Decimal
from itertools import groupby

from .scanner import decimal


def closed_statistics(rows, count):
    result=dict(closed_count=count,complete=False,win_rate_pct=None,profit_factor=None,
        profit_factor_reason='no_closed_positions',mean_net_usdt=None,best_net_usdt=None,worst_net_usdt=None,
        max_drawdown_before_funding_usdt=None,max_drawdown_net_usdt=None,mean_hold_minutes=None,
        points=[],point_limit=281,sampled=False,close_groups=0,
        classification='cumulative_closed_paper_pnl_not_account_equity')
    if not count: return result
    before=net=peak_before=peak_net=draw_before=draw_net=Decimal(0)
    profit=loss=duration=Decimal(0)
    known=wins=seen=0
    best=worst=None
    prefix=True
    bucket_size=max(1,(count+39)//40)
    buckets={}
    origin=None
    for stamp,group in groupby(rows,key=lambda row:row[0]):
        if type(stamp) is not int or stamp<=0: raise ValueError('invalid_paper_close_time')
        group_before=group_net=Decimal(0)
        group_known=True
        group_count=0
        for _,opened,pnl,final in group:
            if type(opened) is not int or not 0<opened<=stamp: raise ValueError('invalid_paper_hold_time')
            duration+=Decimal(stamp-opened)/60000
            group_before+=decimal(pnl)
            group_count+=1
            if final is None:
                group_known=False
            else:
                value=decimal(final)
                known+=1
                group_net+=value
                profit+=max(Decimal(0),value)
                loss+=max(Decimal(0),-value)
                wins+=int(value>0)
                best=value if best is None else max(best,value)
                worst=value if worst is None else min(worst,value)
        bucket=seen//bucket_size
        seen+=group_count
        before+=group_before
        net+=group_net
        peak_before=max(peak_before,before)
        draw_before=max(draw_before,peak_before-before)
        prefix=prefix and group_known
        if prefix:
            peak_net=max(peak_net,net)
            draw_net=max(draw_net,peak_net-net)
        point=dict(ordinal=seen,timestamp_ms=stamp,before_usdt=str(before),net_usdt=str(net) if prefix else None)
        if origin is None:
            origin=dict(ordinal=0,timestamp_ms=stamp,before_usdt='0',net_usdt='0')
        values=buckets.setdefault(bucket,{})
        values.setdefault('first',point)
        values['last']=point
        for key,field,choose in (('low_before','before_usdt',min),('high_before','before_usdt',max),('low_net','net_usdt',min),('high_net','net_usdt',max)):
            if point[field] is None: continue
            previous=values.get(key)
            if previous is None or decimal(point[field])==choose(decimal(point[field]),decimal(previous[field])):
                values[key]=point
        if point['net_usdt'] is None: values.setdefault('gap',point)
        result['close_groups']+=1
    if seen!=count: raise ValueError('inconsistent_paper_closed_count')
    picked={point['ordinal']:point for bucket in buckets.values() for point in bucket.values()}
    result['points']=[origin]+[picked[key] for key in sorted(picked)]
    result.update(complete=known==count,mean_hold_minutes=str(duration/count),
        max_drawdown_before_funding_usdt=str(draw_before),sampled=len(result['points'])<result['close_groups']+1)
    if known==count:
        result.update(win_rate_pct=str(Decimal(wins)*100/count),mean_net_usdt=str(net/count),
            best_net_usdt=str(best),worst_net_usdt=str(worst),max_drawdown_net_usdt=str(draw_net),
            profit_factor=str(profit/loss) if loss else None,profit_factor_reason=None if loss else 'no_losses')
    else:
        result['profit_factor_reason']='incomplete_funding'
    return result
