"""Public USDC order-book feeds for edgeX V2 and Extended."""
import asyncio
from contextlib import suppress

import aiohttp

EDGEX_METADATA = 'https://edgex-prod-v2.edgex.exchange/api/v2/public/meta/getMetaData'
EDGEX_SOCKET = 'wss://edgex-quote-prod-v2.edgex.exchange/api/v1/public/ws'
EDGEX_CHANNEL = 'bookTicker.all.1s'
EXTENDED_INFO = 'https://api.starknet.extended.exchange/api/v1/info'
USDC_ETHEREUM_ID = '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48'


def usdc_market(native_id, base):
    return dict(id=native_id, base=base, symbol=f'{base}/USDC:USDC', quote='USDC',
                settle='USDC', active=True, swap=True, linear=True, contractSize=1)


def parse_edgex_markets(data):
    coins = {coin['coinId']: coin['coinName'] for coin in data['coinList']}
    markets = {}
    for contract in data['contractList']:
        if (contract['enableTrade'] is not True or contract.get('isStock') or contract.get('isFx')
                or coins.get(contract['quoteCoinId']) != 'USDC'):
            continue
        base = coins.get(contract['baseCoinId'])
        if base and contract['contractName'] == base + 'USDC':
            market = usdc_market(contract['contractId'], base)
            markets[market['id']] = market
    if not markets:
        raise ValueError('edgeX returned no USDC perpetuals')
    return markets


async def get_data(session, url, budget, *, background=False):
    await budget.wait(background=background)
    async with session.get(url) as response:
        if response.status == 429:
            budget.limited()
        response.raise_for_status()
        return (await response.json())['data']


class EdgeXPublic:
    def __init__(self, session, *, budget, background=False):
        self.session = session
        self.budget, self.background = budget, background
        self.markets = {}
        self.quotes = {}
        self.changed = asyncio.Event()
        self.reader = None
        self.error = None

    async def load_markets(self, reload=False):
        data = await get_data(self.session, EDGEX_METADATA, self.budget, background=self.background)
        self.markets = parse_edgex_markets(data)
        return self.markets

    async def read_quotes(self):
        try:
            timeout = aiohttp.ClientWSTimeout(ws_receive=8, ws_close=2)
            async with self.session.ws_connect(EDGEX_SOCKET, timeout=timeout) as ws:
                await ws.send_json(dict(type='subscribe', channel=EDGEX_CHANNEL))
                async for message in ws:
                    if message.type != aiohttp.WSMsgType.TEXT:
                        continue
                    event = message.json()
                    if event.get('type') == 'ping':
                        await ws.send_json(dict(type='pong', time=event['time']))
                    elif event.get('type') == 'error':
                        raise ValueError(f"edgeX WebSocket: {event.get('content')}")
                    elif event.get('channel') == EDGEX_CHANNEL and event.get('type') == 'quote-event':
                        self.quotes = {item['contractId']: item for item in event['content']['data']}
                        self.changed.set()
                raise ConnectionError('edgeX WebSocket closed')
        except (aiohttp.ClientError, TimeoutError, ConnectionError, ValueError, KeyError, TypeError) as exc:
            self.error = exc
            self.changed.set()

    async def fetch_tickers(self):
        if self.reader is None or self.reader.done():
            self.error = None
            self.reader = asyncio.create_task(self.read_quotes())
        self.changed.clear()
        await self.changed.wait()
        if self.error:
            raise self.error
        tickers = {}
        for native, item in self.quotes.items():
            market = self.markets.get(native)
            if market is None or item.get('contractName') != market['base'] + 'USDC':
                continue
            symbol = market['symbol']
            tickers[symbol] = dict(symbol=symbol, native_id=native,
                                   bid=item.get('bestBidPrice'), ask=item.get('bestAskPrice'),
                                   timestamp=item.get('time'))
        return tickers

    async def close(self):
        if self.reader is not None:
            self.reader.cancel()
            with suppress(asyncio.CancelledError):
                await self.reader


class ExtendedPublic:
    def __init__(self, session, *, budget, background=False):
        self.session = session
        self.budget, self.background = budget, background
        self.usdc_ids = set()

    def market_for(self, item):
        collateral = (item.get('l2Config') or {}).get('collateralId')
        if (item.get('type') == 'PERPETUAL' and item.get('active') is True
                and item.get('category') == 'Crypto' and item.get('isRfq') is False
                and item.get('isOffHours') is False and collateral in self.usdc_ids
                and item.get('assetName') and item.get('name') == item['assetName'] + '-USD'):
            return usdc_market(item['name'], item['assetName'])
        return None

    async def load_markets(self, reload=False):
        async with asyncio.TaskGroup() as tasks:
            asset_request = tasks.create_task(get_data(self.session, EXTENDED_INFO + '/assets', self.budget,
                                                       background=self.background))
            market_request = tasks.create_task(get_data(self.session, EXTENDED_INFO + '/markets', self.budget,
                                                        background=self.background))
        assets, rows = asset_request.result(), market_request.result()
        self.usdc_ids = {asset['starkexId'] for asset in assets
                         if asset.get('isCollateral') is True and asset.get('l1Id') == USDC_ETHEREUM_ID
                         and asset.get('starkexId')}
        if not self.usdc_ids:
            raise ValueError('Extended USDC collateral identifier is unavailable')
        markets = {market['id']: market for row in rows if (market := self.market_for(row))}
        if not markets:
            raise ValueError('Extended returned no active USDC order-book perpetuals')
        return markets

    async def fetch_tickers(self):
        tickers = {}
        for item in await get_data(self.session, EXTENDED_INFO + '/markets', self.budget):
            market = self.market_for(item)
            if market:
                symbol = market['symbol']
                stats = item['marketStats']
                tickers[symbol] = dict(symbol=symbol, native_id=market['id'], bid=stats.get('bidPrice'),
                                       ask=stats.get('askPrice'), timestamp=None, last=stats.get('lastPrice'),
                                       quoteVolume=stats.get('dailyVolume'), indexPrice=stats.get('indexPrice'),
                                       fundingRate=stats.get('fundingRate'), fundingTimestamp=stats.get('nextFundingRate'),
                                       openInterestValue=stats.get('openInterest'), interval='1h')
        return tickers

    async def close(self):
        pass  # The monitor owns the shared HTTP session.
