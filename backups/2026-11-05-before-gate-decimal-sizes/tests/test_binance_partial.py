import asyncio
import time
import unittest
from unittest.mock import patch
from aiohttp import web
from arbitrage.live import LiveExchange
from arbitrage.streams import parse, subscriptions


MARKETS={'BTCUSDT':dict(id='BTCUSDT',symbol='BTC/USDT:USDT',base='BTC',quote='USDT',contractSize=1)}


def frame(bids=None,asks=None):
    return dict(stream='btcusdt@depth5@100ms',data=dict(e='depthUpdate',s='BTCUSDT',T=10000,E=10001,u=123,st=1,
        b=[['100','2'],['99','3']] if bids is None else bids,
        a=[['101','2'],['102','3']] if asks is None else asks))


class BinancePartialTests(unittest.TestCase):
    def quote(self,message):return parse('binanceusdm',message,MARKETS,10100,'depth5@100ms')[0]

    def test_partial_top_uses_transaction_time_and_preserves_source_quality(self):
        q=self.quote(frame())
        self.assertEqual((q.bid,q.ask,q.source_timestamp_ms),(100,101,10000))
        self.assertEqual(q.source,'ws_bbo')
        self.assertEqual(q.quality(10100),'ok')
        self.assertEqual(q.quality(14000),'stale')

    def test_empty_or_zero_side_is_not_filled_from_lower_level(self):
        self.assertIsNone(self.quote(frame(bids=[])).bid)
        self.assertIsNone(self.quote(frame(bids=[['100','0'],['99','3']])).bid)
        self.assertEqual(self.quote(frame(asks=[])).quality(10100),'missing_bid_ask')

    def test_diff_topic_raw_message_and_wrong_symbol_are_rejected(self):
        for kind in ('diff','raw','symbol'):
            with self.subTest(kind=kind):
                value=frame()
                if kind=='diff':value['stream']='btcusdt@depth@100ms'
                elif kind=='raw':value=value['data']
                else:value['data']['s']='ETHUSDT'
                with self.assertRaises(ValueError):self.quote(value)

    def test_non_um_payload_and_invalid_update_id_are_rejected(self):
        for key,value in (('st',2),('u',True),('u',-1),('u','123')):
            item=frame();item['data'][key]=value
            with self.subTest(key=key,value=value),self.assertRaises(ValueError):self.quote(item)
        with self.assertRaises(ValueError):
            parse('binanceusdm',dict(e='bookTicker',s='BTCUSDT',st=2,b='100',a='101',T=10000),MARKETS,10100)

    def test_ordering_and_oversized_snapshot_are_rejected(self):
        for bids in ([['99','1'],['100','1']],[['100','1'],['100','1']],[[str(100-i),'1'] for i in range(6)]):
            with self.subTest(bids=bids),self.assertRaises(ValueError):self.quote(frame(bids=bids))

    def test_replacement_can_move_best_price_away_from_previous_snapshot(self):
        self.assertEqual(self.quote(frame()).bid,100)
        self.assertEqual(self.quote(frame(bids=[['98','2']])).bid,98)

    def test_subscription_and_control_frames_are_bound_to_selected_mode(self):
        self.assertEqual(subscriptions('binanceusdm',['BTCUSDT'],'depth5@100ms')[0]['params'],['btcusdt@depth5@100ms'])
        self.assertEqual(subscriptions('binanceusdm',['BTCUSDT'],'bookTicker-combined')[0]['params'],['btcusdt@bookTicker'])
        self.assertEqual(parse('binanceusdm',{'id':1,'result':None},MARKETS,10100,'depth5@100ms'),[])

    def test_combined_book_ticker_requires_matching_topic(self):
        message=dict(stream='btcusdt@bookTicker',data=dict(e='bookTicker',s='BTCUSDT',u=1,st=1,b='100',a='101',T=10000))
        self.assertEqual(parse('binanceusdm',message,MARKETS,10100,'bookTicker-combined')[0].bid,100)
        message['stream']='ethusdt@bookTicker'
        with self.assertRaises(ValueError):parse('binanceusdm',message,MARKETS,10100,'bookTicker-combined')


class BinanceSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_combined_socket_resubscribes_and_resets_update_id_on_reconnect(self):
        connections=[];drop=asyncio.Event()
        async def handler(request):
            socket=web.WebSocketResponse();await socket.prepare(request)
            subscription=await socket.receive_json();connections.append(subscription)
            value=frame();value['stream']='btcusdt@depth5@500ms'
            value['data'].update(u=900 if len(connections)==1 else 2,T=int(time.time()*1000))
            if len(connections)>1:value['data'].update(b=[['200','2']],a=[['201','2']])
            await socket.send_json(value)
            if len(connections)==1:
                await drop.wait();await socket.close()
            else:await socket.receive()
            return socket
        app=web.Application();app.router.add_get('/stream',handler)
        runner=web.AppRunner(app);await runner.setup()
        site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
        worker=LiveExchange('binanceusdm',{'BTC'});worker.markets=MARKETS
        self.assertEqual(worker.binance_stream,'depth5@500ms')
        async def until(condition):
            async with asyncio.timeout(10):
                while not condition():await asyncio.sleep(.01)
        with patch('arbitrage.live.BINANCE_COMBINED_URL',f'http://127.0.0.1:{runner.addresses[0][1]}/stream'):
            task=asyncio.create_task(worker.run())
            try:
                await until(lambda:bool(worker.quotes))
                self.assertEqual(next(iter(worker.quotes.values())).bid,100)
                drop.set()
                await until(lambda:worker.state=='reconnecting')
                self.assertEqual(worker.snapshot(int(time.time()*1000))['status']['usable_quotes'],0)
                await until(lambda:len(connections)>1 and worker.state=='streaming')
                self.assertEqual(next(iter(worker.quotes.values())).bid,200)
                self.assertEqual(worker.binance_update_ids['BTCUSDT'],2)
                self.assertEqual(connections[0]['params'],['btcusdt@depth5@500ms'])
                self.assertEqual(connections[0],connections[1])
            finally:
                drop.set();task.cancel();await asyncio.gather(task,return_exceptions=True);await runner.cleanup()

    async def test_duplicate_or_reversed_update_id_cannot_refresh_quote(self):
        worker=LiveExchange('binanceusdm',{'BTC'},'depth5@100ms');worker.markets=MARKETS
        self.assertEqual(len(await worker.parse_quotes(None,frame(),10100)),1)
        self.assertEqual(await worker.parse_quotes(None,frame(),10101),[])
        value=frame();value['data']['u']=122
        self.assertEqual(await worker.parse_quotes(None,value,10102),[])
        value['data']['u']=125
        self.assertEqual(len(await worker.parse_quotes(None,value,10103)),1)
        self.assertEqual(worker.out_of_order_updates,2)
