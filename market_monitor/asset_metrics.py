"""Market capitalization and FDV for assets with explicit CoinGecko IDs."""

import asyncio
from datetime import datetime
import logging
import os

import aiohttp

from .market import fresh, now_ms, numeric


URL = 'https://api.coingecko.com/api/v3/coins/markets'
REFRESH_SECONDS = 600
MAX_AGE_MS = 30 * 60_000
LOG = logging.getLogger(__name__)


def positive_millions(value):
    value = numeric(value)
    return value / 1_000_000 if value is not None and value > 0 else None


def parse_valuations(rows, assets_by_id: dict[str, str], now: int):
    if not isinstance(rows, list):
        raise ValueError('Expected a list of CoinGecko markets')
    values, seen = {}, set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), str):
            raise ValueError('Invalid CoinGecko market')
        asset = assets_by_id.get(row['id'])
        if asset is None:
            continue
        if asset in seen:
            raise ValueError('Duplicate CoinGecko market')
        seen.add(asset)
        try:
            stamp = datetime.fromisoformat(row.get('last_updated'))
        except (TypeError, ValueError):
            continue
        if stamp.tzinfo is None:
            continue
        updated_at = int(stamp.timestamp() * 1000)
        if not fresh(updated_at, now, MAX_AGE_MS):
            continue
        cap = positive_millions(row.get('market_cap'))
        fdv = positive_millions(row.get('fully_diluted_valuation'))
        if cap is not None or fdv is not None:
            values[asset] = dict(cap=cap, fdv=fdv, updatedAt=updated_at, expiresAt=updated_at + MAX_AGE_MS)
    return values


class AssetMetrics:
    def __init__(self, provider_ids: dict[str, str]):
        self.assets_by_id = {provider_id: asset for asset, provider_id in provider_ids.items()}
        key = os.environ.get('COINGECKO_DEMO_API_KEY')
        self.headers = {'x-cg-demo-api-key': key} if key else {}
        self.values = {}

    async def refresh(self, session: aiohttp.ClientSession):
        ids = list(self.assets_by_id)
        for start in range(0, len(ids), 100):
            assets = {provider_id: self.assets_by_id[provider_id] for provider_id in ids[start:start + 100]}
            params = dict(vs_currency='usd', ids=','.join(assets), per_page='100')
            try:
                async with session.get(URL, params=params, headers=self.headers) as response:
                    response.raise_for_status()
                    values = parse_valuations(await response.json(), assets, now_ms())
            except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
                LOG.warning('CoinGecko valuation refresh: %s', str(exc)[:200])
                return
            for asset in assets.values():
                self.values.pop(asset, None)
            self.values.update(values)

    def snapshot(self, now: int):
        return {asset: value for asset, value in self.values.items() if now < value['expiresAt']}

    async def run(self, session: aiohttp.ClientSession):
        while self.assets_by_id:
            await self.refresh(session)
            await asyncio.sleep(REFRESH_SECONDS)
