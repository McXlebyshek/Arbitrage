from copy import deepcopy
from contextlib import redirect_stdout
from io import StringIO
import json
from decimal import Decimal
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from arbitrage.settlement_marks import BINANCE_SOURCE, BINANCE_URL, fetch_binance_marks, fetch_position_prices, load_position
from arbitrage.scanner import Registry
import test_paper_funding as funding_tests
from test_economics import NOW, HOUR


def row(stamp=NOW, **extra):
    return dict(symbol='BTCUSDT',fundingTime=stamp,fundingRate='.001',markPrice='100',**extra)


class Response:
    def __init__(self,rows,status=200): self.rows,self.status=rows,status
    async def __aenter__(self): return self
    async def __aexit__(self,*unused): pass
    async def json(self): return self.rows


class Session:
    def __init__(self,responses): self.responses,self.calls=list(responses),[]
    def get(self,url,**kwargs):
        self.calls.append((url,kwargs))
        value=self.responses.pop(0)
        if isinstance(value,Exception): raise value
        return value


class SettlementMarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_exact_mark_from_funding_endpoint_and_request_contract(self):
        session=Session([Response([row()])])
        result=await fetch_binance_marks(session,'BTCUSDT',{NOW:'.001'})
        self.assertEqual(result['prices'][0]['mark_price_native'],'100')
        self.assertEqual(result['prices'][0]['source'],BINANCE_SOURCE)
        self.assertEqual(result['missing'],[])
        url,kwargs=session.calls[0]
        self.assertEqual(url,BINANCE_URL)
        self.assertEqual(kwargs['params'],dict(symbol='BTCUSDT',startTime=NOW,endTime=NOW,limit=1000))
        self.assertFalse(kwargs['allow_redirects'])
        self.assertEqual(kwargs['timeout'].total,15)

    async def test_instrument_time_special_rate_mismatch_and_bad_price_rejected(self):
        for change in (dict(symbol='ETHUSDT'),dict(fundingTime=NOW+1),dict(rateType='Special'),
                dict(fundingRate='.002'),dict(markPrice='0'),dict(markPrice='NaN')):
            payload=dict(row(),**change)
            result=await fetch_binance_marks(Session([Response([payload])]),'BTCUSDT',{NOW:'.001'})
            self.assertIsNotNone(result['error'])
            self.assertEqual(result['prices'],[])

    async def test_absent_mark_is_missing_and_never_filled_from_other_price_field(self):
        payload=row();payload.pop('markPrice');payload['price']='999'
        result=await fetch_binance_marks(Session([Response([payload])]),'BTCUSDT',{NOW:'.001'})
        self.assertEqual(result['prices'],[])
        self.assertEqual(result['missing'],[NOW])
        self.assertIsNone(result['error'])

    async def test_conflicting_page_is_rejected_atomically_but_identical_duplicates_work(self):
        result=await fetch_binance_marks(Session([Response([row(),dict(row(),markPrice='101')])]),'BTCUSDT',{NOW:'.001'})
        self.assertEqual(result['prices'],[])
        self.assertEqual(result['error'],'conflicting_settlement_rows')
        result=await fetch_binance_marks(Session([Response([row(),dict(row(),markPrice='100.00')])]),'BTCUSDT',{NOW:'.001'})
        self.assertEqual(len(result['prices']),1)

    async def test_http_rate_limit_bad_format_and_transport_stop_without_retry(self):
        for value in (Response([],429),Response({},200),aiohttp.ClientError('transport')):
            session=Session([value])
            result=await fetch_binance_marks(session,'BTCUSDT',{NOW:'.001'})
            self.assertIsNotNone(result['error'])
            self.assertEqual(result['requests'],1)
            self.assertEqual(len(session.calls),1)

    async def test_pagination_is_bounded_and_advances_cursor(self):
        first=[row(NOW+i*60000) for i in range(1000)]
        end=NOW+1000*60000
        session=Session([Response(first),Response([row(end)])])
        with patch('arbitrage.settlement_marks.asyncio.sleep'):
            result=await fetch_binance_marks(session,'BTCUSDT',{NOW:'.001',end:'.001'})
        self.assertEqual(len(result['prices']),2)
        self.assertEqual(session.calls[1][1]['params']['startTime'],NOW+999*60000+1)
        result=await fetch_binance_marks(Session([Response(first)]),'BTCUSDT',{NOW:'.001',end:'.001'},max_pages=1)
        self.assertEqual(result['error'],'settlement_page_budget_exhausted')
        self.assertEqual(result['missing'],[end])

    async def test_invalid_or_empty_requests_do_not_contact_network(self):
        session=Session([])
        self.assertEqual((await fetch_binance_marks(session,'BTCUSDT',{}))['requests'],0)
        for native,events in (('../bad',{NOW:'.001'}),('BTCUSDT',{True:'.001'}),('BTCUSDT',{NOW+10*HOUR:'.001'})):
            with self.assertRaises(ValueError): await fetch_binance_marks(session,native,events,now_ms=NOW)
        self.assertEqual(session.calls,[])


class PositionDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture=funding_tests.PaperFundingTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.position=deepcopy(self.fixture.position)
        self.position['pair']['long_exchange']='binanceusdm'
        self.position['pair']['long_symbol']='BTCUSDT'
        self.position['bindings']['long'].update(exchange='binanceusdm',id='BTCUSDT')
        self.registry=deepcopy(self.fixture.registry)
        binding=self.registry.bindings.pop(('alpha','BTC_USDT'))
        binding.update(exchange='binanceusdm',id='BTCUSDT')
        self.registry.bindings[('binanceusdm','BTCUSDT')]=binding
        from contextlib import closing
        import sqlite3
        with closing(sqlite3.connect(self.fixture.market)) as db,db:
            db.execute("UPDATE funding_history_events SET exchange='binanceusdm',native_id='BTCUSDT' WHERE exchange='alpha'")
            db.execute("UPDATE funding_history_imports SET exchange='binanceusdm',native_id='BTCUSDT',payload=json_set(payload,'$.exchange','binanceusdm','$.native_id','BTCUSDT') WHERE exchange='alpha'")

    async def test_supported_leg_download_and_unsupported_leg_stays_missing(self):
        session=Session([Response([row(NOW+8*HOUR)])])
        result=await fetch_position_prices(self.position,self.fixture.market,self.registry,session=session)
        self.assertEqual(result['report']['downloaded'],1)
        self.assertEqual(len(result['report']['unsupported']),2)
        from arbitrage.paper_funding import reconcile
        funding=reconcile(self.position,self.fixture.market,result['prices'])
        self.assertFalse(funding['complete'])
        self.assertIsNone(funding['funding_usdt'])

    async def test_manual_other_leg_combines_to_complete_and_matching_cache_skips_network(self):
        supplied=self.fixture.prices[1:]
        result=await fetch_position_prices(self.position,self.fixture.market,self.registry,supplied,session=Session([Response([row(NOW+8*HOUR)])]))
        from arbitrage.paper_funding import reconcile
        funding=reconcile(self.position,self.fixture.market,result['prices'])
        self.assertTrue(funding['complete'])
        self.position['funding']=funding
        cached=await fetch_position_prices(self.position,self.fixture.market,self.registry,supplied,session=Session([]))
        self.assertEqual(cached['report']['cached'],1)
        self.assertEqual(cached['report']['requests'],0)
        refreshed=await fetch_position_prices(self.position,self.fixture.market,self.registry,supplied,True,Session([Response([row(NOW+8*HOUR)])]))
        self.assertEqual(refreshed['report']['requests'],1)

    async def test_registry_change_blocks_download_and_load_position_is_read_only(self):
        before=self.fixture.path.read_bytes()
        loaded=load_position(self.fixture.path,self.fixture.position['id'],True)
        self.assertEqual(loaded['id'],self.fixture.position['id'])
        self.assertEqual(before,self.fixture.path.read_bytes())
        with self.assertRaises(ValueError): load_position(self.fixture.path,self.fixture.position['id'],False)
        self.position['bindings']['long']['price_units']='1000'
        session=Session([])
        with self.assertRaises(ValueError): await fetch_position_prices(self.position,self.fixture.market,self.registry,session=session)
        self.assertEqual(session.calls,[])

    async def test_revised_archive_rate_invalidates_cached_mark(self):
        from contextlib import closing
        import sqlite3
        from arbitrage.paper_funding import reconcile
        result=await fetch_position_prices(self.position,self.fixture.market,self.registry,self.fixture.prices[1:],
            session=Session([Response([row(NOW+8*HOUR)])]))
        self.position['funding']=reconcile(self.position,self.fixture.market,result['prices'])
        with closing(sqlite3.connect(self.fixture.market)) as db,db:
            db.execute("UPDATE funding_history_events SET rate='-.001' WHERE exchange='binanceusdm' AND timestamp_ms=?",(NOW+8*HOUR,))
        result=await fetch_position_prices(self.position,self.fixture.market,self.registry,self.fixture.prices[1:],
            session=Session([Response([dict(row(NOW+8*HOUR),fundingRate='-.001')])]))
        self.assertEqual(result['report']['cached'],0)
        self.assertEqual(result['report']['requests'],1)
        # A further archive revision between download and reconciliation cannot use the old mark.
        with closing(sqlite3.connect(self.fixture.market)) as db,db:
            db.execute("UPDATE funding_history_events SET rate='-.002' WHERE exchange='binanceusdm' AND timestamp_ms=?",(NOW+8*HOUR,))
        funding=reconcile(self.position,self.fixture.market,result['prices'])
        self.assertFalse(funding['complete'])
        self.assertIn('settlement_rate_changed_after_download',[item['reason'] for item in funding['missing_mark_prices']])


class DownloadCLITests(unittest.TestCase):
    def test_funding_cli_download_then_reconcile_records_prices_and_report(self):
        from arbitrage.paper import main
        fixture=funding_tests.PaperFundingTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        output=StringIO()
        fake=AsyncMock(return_value=dict(prices=fixture.prices,report=dict(downloaded=3,requests=1)))
        argv=['paper','funding','--replay','--database',str(fixture.path),'--market-database',str(fixture.market),
            '--position',fixture.position['id'],'--fetch-prices']
        with patch('sys.argv',argv),patch('arbitrage.settlement_marks.fetch_position_prices',fake),patch('arbitrage.paper.Registry.load',return_value=fixture.registry),redirect_stdout(output):
            main()
        result=json.loads(output.getvalue())
        self.assertTrue(result['funding']['complete'])
        self.assertEqual(result['price_download']['downloaded'],3)
        self.assertIsNotNone(result['valuation']['net_pnl_usdt'])
        self.assertEqual(fake.await_count,1)
