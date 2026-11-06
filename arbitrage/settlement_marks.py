"""Bounded public settlement-price download. Exact funding marks, never candle proxies."""
import asyncio
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import time

import aiohttp

from .paper import identity
from .paper_funding import price_index, reconcile
from .scanner import decimal, positive

BINANCE_URL = 'https://fapi.binance.com/fapi/v1/fundingRate'
BINANCE_SOURCE = 'binance_funding_history_markPrice:'+BINANCE_URL


async def fetch_binance_marks(session, native_id, expected, max_pages=3, now_ms=None):
    now = int(time.time()*1000) if now_ms is None else now_ms
    if not re.fullmatch(r'[A-Z0-9_]{1,36}USDT', native_id) or type(max_pages) is not int or not 1 <= max_pages <= 3:
        raise ValueError('invalid_settlement_download_request')
    if len(expected) > 2000 or any(type(stamp) is not int or stamp <= 0 or stamp > now for stamp in expected):
        raise ValueError('invalid_settled_event_window')
    rates = {stamp:decimal(rate) for stamp,rate in expected.items()}
    result = dict(prices=[],requests=0,missing=sorted(expected),error=None,retry_after_seconds=0)
    if not expected: return result
    cursor, end = min(expected), max(expected)
    found = {}
    for page in range(max_pages):
        if page: await asyncio.sleep(.75)
        try:
            result['requests'] += 1
            async with session.get(BINANCE_URL, params=dict(symbol=native_id,startTime=cursor,endTime=end,limit=1000),
                    timeout=aiohttp.ClientTimeout(total=15),allow_redirects=False) as response:
                if response.status != 200:
                    if response.status in (418,429):
                        raw = getattr(response,'headers',{}).get('Retry-After','300')
                        result['retry_after_seconds'] = min(3600,max(60,int(raw))) if str(raw).isdigit() else 300
                    raise ValueError('settlement_http_'+str(response.status))
                rows = await response.json()
            if not isinstance(rows,list) or len(rows)>1000:
                raise ValueError('invalid_settlement_response')
            if not rows: break
            page_values = {}
            for row in rows:
                stamp = row.get('fundingTime')
                if (row.get('symbol') != native_id or type(stamp) is not int or not cursor <= stamp <= end
                        or row.get('rateType','Regular') != 'Regular'):
                    raise ValueError('settlement_identity_time_or_type_mismatch')
                rate = decimal(row.get('fundingRate'))
                raw_price = row.get('markPrice')
                price = positive(raw_price) if raw_price not in (None,'') else None
                value = (rate,price)
                if stamp in page_values and page_values[stamp] != value:
                    raise ValueError('conflicting_settlement_rows')
                page_values[stamp] = value
            # Do not accept any prices from a malformed/conflicting page.
            for stamp,(rate,price) in page_values.items():
                if stamp not in rates: continue
                if rate != rates[stamp]:
                    raise ValueError('settlement_rate_differs_from_archive')
            for stamp,(rate,price) in page_values.items():
                if stamp in rates and price is not None:
                    found[stamp] = dict(exchange='binanceusdm',native_id=native_id,timestamp_ms=stamp,
                        mark_price_native=str(price),source=BINANCE_SOURCE,settled_rate=str(rate))
            cursor = max(page_values)+1
            if cursor > end or len(rows)<1000 or all(stamp in found for stamp in rates): break
        except (ValueError,TypeError,KeyError,aiohttp.ClientError,asyncio.TimeoutError) as exc:
            result['error'] = str(exc) if isinstance(exc,ValueError) else 'settlement_transport_or_format_error'
            break
    else:
        result['error'] = 'settlement_page_budget_exhausted'
    result['prices'] = [found[stamp] for stamp in sorted(found)]
    result['missing'] = sorted(set(rates)-set(found))
    return result


def load_position(database, position_id, replay=False):
    path = Path(database)
    if not path.is_file(): raise ValueError('paper_position_not_found')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        db.execute('BEGIN')
        mode = db.execute('SELECT mode FROM paper_meta').fetchone()
        if not mode or mode[0] != ('replay' if replay else 'live_snapshot'):
            raise ValueError('paper_live_and_replay_require_separate_ledgers')
        row = db.execute('SELECT payload FROM paper_positions WHERE id=?',(position_id,)).fetchone()
        if not row: raise ValueError('paper_position_not_found')
        position = json.loads(row[0])
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='paper_settlement_prices'").fetchone():
            cached=[]
            for side in ('long','short'):
                rows=db.execute('SELECT exchange,native_id,timestamp_ms,rate,mark_price_native,source FROM paper_settlement_prices WHERE exchange=? AND native_id=? AND timestamp_ms>? AND timestamp_ms<=? ORDER BY timestamp_ms LIMIT 2000',
                    (position['pair'][side+'_exchange'],position['pair'][side+'_symbol'],position['opened_at_ms'],position['valuation']['timestamp_ms']))
                cached.extend(dict(exchange=x,native_id=n,timestamp_ms=t,rate=r,mark_price_native=p,mark_price_source=s) for x,n,t,r,p,s in rows)
            position['_settlement_cache']=cached
        return position


async def fetch_position_prices(position, market_database, registry, supplied=None, refresh=False, session=None, max_requests=3):
    if type(max_requests) is not int or not 0<=max_requests<=3:
        raise ValueError('invalid_settlement_request_budget')
    if identity(position['pair'],registry) != position['bindings']:
        raise ValueError('paper_instrument_identity_changed')
    supplied = [] if supplied is None else list(supplied)
    manual = price_index(supplied)
    baseline = reconcile(position,market_database,supplied)
    current = {(row['exchange'],row['native_id'],row['timestamp_ms']):row for row in baseline['events']}
    cached = []
    if not refresh:
        previous = {(row['exchange'],row['native_id'],row['timestamp_ms']):row
            for row in position.get('funding',{}).get('events',[])+position.get('_settlement_cache',[])}
        for row in previous.values():
            key = row['exchange'],row['native_id'],row['timestamp_ms']
            if (key not in manual and key in current and row.get('mark_price_source') == BINANCE_SOURCE
                    and row.get('mark_price_native') is not None and decimal(row['rate']) == decimal(current[key]['rate'])):
                cached.append(dict(exchange=key[0],native_id=key[1],timestamp_ms=key[2],mark_price_native=row['mark_price_native'],source=BINANCE_SOURCE,settled_rate=row['rate']))
    prices = supplied+cached
    indexed = price_index(prices)
    grouped, unsupported = {}, []
    for key,row in current.items():
        if key in indexed: continue
        if key[0] != 'binanceusdm':
            unsupported.append(dict(exchange=key[0],native_id=key[1],timestamp_ms=key[2],reason='exact_settlement_mark_adapter_unavailable'))
        else:
            grouped.setdefault(key[1],{})[key[2]] = row['rate']
    report = dict(requests=0,cached=len(cached),downloaded=0,unsupported=unsupported,downloads=[],retry_after_seconds=0,
        classification='public_settlement_mark_download',execution_verified=False)
    if session is None:
        async with aiohttp.ClientSession() as own_session:
            return await _collect(own_session,grouped,prices,report,max_requests)
    return await _collect(session,grouped,prices,report,max_requests)


async def _collect(session, grouped, prices, report,max_requests=3):
    for index,(native_id,events) in enumerate(sorted(grouped.items())):
        remaining=max_requests-report['requests']
        if not remaining:
            report['downloads'].append(dict(native_id=native_id,requests=0,missing=sorted(events),error='network_budget_deferred'))
            continue
        if index: await asyncio.sleep(.75)
        result = await fetch_binance_marks(session,native_id,events,max_pages=min(3,remaining))
        prices.extend(result['prices'])
        report['requests'] += result['requests']
        report['downloaded'] += len(result['prices'])
        report['retry_after_seconds']=max(report['retry_after_seconds'],result['retry_after_seconds'])
        report['downloads'].append(dict(native_id=native_id,requests=result['requests'],missing=result['missing'],error=result['error']))
        # One rate-limit/transport failure must not fan out into further requests.
        if result['error']:
            for omitted in sorted(set(grouped)-{row['native_id'] for row in report['downloads']}):
                report['downloads'].append(dict(native_id=omitted,requests=0,missing=sorted(grouped[omitted]),error='not_requested_after_failure'))
            break
    return dict(prices=prices,report=report)
