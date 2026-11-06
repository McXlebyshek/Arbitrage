"""Read-only loopback dashboard for the local arbitrage monitor."""
import argparse
import asyncio
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import time
import webbrowser

from aiohttp import web
from .scanner import Registry
from .history import read_events

WEB = Path(__file__).with_name("web")


def number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def spread(first, second):
    # Same denominator as the reverse directed scanner row: original SHORT ask.
    return 100*(first-second)/second if first and second else None


class Dashboard:
    def __init__(self, snapshot, database, registry=None, clock=None):
        self.snapshot_path, self.database = Path(snapshot), Path(database)
        self.registry, self.clock = registry or Registry.load(), clock or (lambda: int(time.time()*1000))
        self.archive_cache = None

    def connect(self):
        return sqlite3.connect(self.database.resolve().as_uri()+"?mode=ro", uri=True, timeout=0.1)

    def archive(self):
        if not self.database.is_file():
            return None
        modified = self.database.stat().st_mtime_ns
        if self.archive_cache and self.archive_cache[0] == modified:
            return self.archive_cache[1]
        with closing(self.connect()) as db:
            names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if "scans" not in names:
                return None
            scan = next((value for (payload,) in db.execute("SELECT payload FROM scans ORDER BY id DESC LIMIT 50")
                         if (value := json.loads(payload)).get("rows")), None)
            if scan is None:
                return None
            timestamp = scan["generated_at_ms"]
            def related(table):
                row = db.execute(f"SELECT payload FROM {table} WHERE generated_at_ms=? ORDER BY id DESC LIMIT 1", (timestamp,)).fetchone() if table in names else None
                return json.loads(row[0]) if row else None
            economics, signals = related("economics"), related("signal_snapshots")
            exchanges = []
            if "snapshots" in names:
                for (exchange,) in db.execute("SELECT DISTINCT exchange FROM snapshots"):
                    row = db.execute("SELECT payload FROM snapshots WHERE exchange=? AND collected_at_ms<=? ORDER BY collected_at_ms DESC,id DESC LIMIT 1", (exchange, timestamp)).fetchone()
                    if row:
                        exchanges.append(json.loads(row[0]))
        result = dict(generated_at_ms=timestamp, scanner=scan, economics=economics, signals=signals, exchanges=exchanges)
        self.archive_cache = (modified, result)
        return result

    def state(self):
        now, document, error = self.clock(), None, None
        try:
            if self.snapshot_path.is_file():
                document = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
                if not isinstance(document, dict):
                    raise ValueError("Invalid snapshot root")
        except (OSError, ValueError) as exc:
            error = f"Не удалось прочитать текущий снимок: {type(exc).__name__}"
            document = None
        original = document
        stopped = bool(document and document.get("exchanges") and all(item["status"]["state"] == "stopped" for item in document["exchanges"]))
        historical = False
        if not document or not (document.get("scanner") or {}).get("rows"):
            try:
                archived = self.archive()
                if archived:
                    document, historical = archived, True
            except (sqlite3.Error, ValueError, OSError) as exc:
                error = f"История недоступна: {type(exc).__name__}"
        if not document:
            return dict(mode="empty", rows=[], exchanges=[], server_time_ms=now, generated_at_ms=None,
                        error=error, economics_policy=None, maintenance=None)
        timestamp = document.get("generated_at_ms", 0)
        mode = "archive" if historical else "stopped" if stopped else "live" if -1000 <= now-timestamp <= 5000 else "stale"
        economic = document.get("economics") or {}
        economics = {row["id"]: row for row in economic.get("rows", [])}
        signals = {row["id"]: row for row in (document.get("signals") or {}).get("rows", [])}
        quotes = {(exchange["status"]["exchange"], row["exchange_symbol"]): row for exchange in document.get("exchanges", []) for row in exchange.get("quotes", [])}
        funds = {(row["exchange"], row["native_id"]): row for row in economic.get("funding_observations", [])}
        inputs = {(row["exchange"], row["native_id"]): row for row in economic.get("inputs", [])}
        result = []
        for raw in (document.get("scanner") or {}).get("rows", []):
            legs = {}
            for side in ("long", "short"):
                key = raw[f"{side}_exchange"], raw[f"{side}_symbol"]
                binding = self.registry.bindings.get(key)
                quote, funding, extra = quotes.get(key, {}), funds.get(key), inputs.get(key, {})
                units = float(binding["price_units"]) if binding else 1
                bid, ask = number(quote.get("bid")), number(quote.get("ask"))
                bid, ask = bid/units if bid else None, ask/units if ask else None
                if side == "long":
                    ask = raw["long_ask_usdt_per_unit"]
                else:
                    bid = raw["short_bid_usdt_per_unit"]
                funding = funding or extra.get("funding")
                history = funding.get("history", []) if funding else []
                funding24 = None
                if funding and funding.get("history_24h_covered"):
                    rates = [number(event.get("rate")) for event in history if timestamp-86400000 < event["timestamp"] <= timestamp]
                    if rates and all(rate is not None for rate in rates):
                        funding24 = sum(rates)*100
                liquidity = extra.get("liquidity") or {}
                legs[side] = dict(exchange=key[0], native_id=key[1], symbol=quote.get("symbol", key[1]),
                    base=binding["base"] if binding else quote.get("base", raw["asset"]), bid=bid, ask=ask,
                    source_time_ms=raw.get(f"{side}_source_timestamp_ms"), received_at_ms=quote.get("received_at_ms"),
                    funding_rate_pct=number(funding.get("rate"))*100 if funding and number(funding.get("rate")) is not None else None,
                    funding_interval_ms=funding.get("interval_ms") if funding else None,
                    funding_timestamp_ms=funding.get("funding_timestamp_ms") if funding else None,
                    funding_received_at_ms=funding.get("received_at_ms") if funding else None,
                    funding_history_24h_pct=funding24,
                    volume_24h_usdt=liquidity.get("quote_volume_24h_usdt"),
                    volume_received_at_ms=liquidity.get("received_at_ms"))
            fresh = mode == "live" and all(leg["source_time_ms"] is not None and -1000 <= now-leg["source_time_ms"] <= 3000
                and leg["received_at_ms"] is not None and -1000 <= now-leg["received_at_ms"] <= 3000 for leg in legs.values())
            model, signal = economics.get(raw["id"]), signals.get(raw["id"])
            economic_fresh = fresh and model is not None and model.get("status") == "scenario_estimated"
            if economic_fresh:
                economic_fresh = all(-1000 <= now-stamp <= 3000 for stamp in model.get("depth_source_times", {}).values()) and len(model.get("depth_source_times", {})) == 2
                if model.get("hold_hours", 0) > 0:
                    economic_fresh = economic_fresh and all(leg.get("funding_received_at_ms") is not None and -1000 <= now-leg["funding_received_at_ms"] <= 120000 for leg in legs.values())
            result.append(dict(id=raw["id"], asset=raw["asset"], symbol=legs["long"]["base"], legs=legs,
                open_spread_pct=raw["open_spread_pct"], exit_spread_pct=spread(legs["long"]["bid"], legs["short"]["ask"]),
                funding_net_24h_pct=(legs["short"]["funding_history_24h_pct"]-legs["long"]["funding_history_24h_pct"])
                    if all(leg["funding_history_24h_pct"] is not None for leg in legs.values()) else None,
                economics=model, signal=signal, data_fresh=bool(fresh), economic_fresh=bool(economic_fresh),
                confirmed_current=bool(economic_fresh and signal and signal["state"] == "confirmed"),
                historical=historical, execution_verified=False))
        return dict(mode=mode, rows=result, exchanges=[item["status"] for item in document.get("exchanges", [])],
                    generated_at_ms=timestamp, server_time_ms=now, error=error,
                    economics_policy=economic.get("policy"), maintenance=(original or {}).get("maintenance"),
                    active_state="stopped" if stopped else "unavailable" if original is None else "running",
                    execution_verified=False)

    def pair(self, identity, minutes, days=7, end_ms=None):
        if minutes not in (1, 15, 60, 360, 1440):
            raise ValueError("Unsupported interval")
        if days not in (1, 3, 7, 14, 28, 30):
            raise ValueError("Unsupported funding window")
        state = self.state()
        item = next((row for row in state["rows"] if row["id"] == identity), None)
        if item is None:
            raise KeyError("Связка отсутствует в выбранном снимке")
        end = state["generated_at_ms"] if state["mode"] != "live" else self.clock()
        if end_ms is not None and (not isinstance(end_ms, int) or isinstance(end_ms, bool) or not 0 <= end_ms <= 8640000000000000):
            raise ValueError('Invalid chart end time')
        chart_end = min(end, end_ms) if end_ms is not None else end
        chart_start = max(0, chart_end-minutes*60000)
        earliest, funding_earliest = None, None
        points, events, funding_series = [], [], []
        observed_funding = {"long": [], "short": []}
        history_source, history_pending = "raw", {}
        comparison = None
        if self.database.is_file():
            with closing(self.connect()) as db:
                db.execute('BEGIN')
                from .funding_comparison import compare
                comparison = compare(db, item['legs'], end, days)
                names = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if 'scans' in names:
                    earliest = db.execute('SELECT min(generated_at_ms) FROM scans').fetchone()[0]
                if 'chart_rollups' in names:
                    first = db.execute('SELECT min(start_ms) FROM chart_rollups WHERE series=?', ('pair:'+identity,)).fetchone()[0]
                    earliest = min(x for x in (earliest, first) if x is not None) if first is not None else earliest
                use_minutes = minutes >= 15 and "chart_rollups" in names
                snapshots = db.execute("SELECT generated_at_ms,payload FROM scans WHERE generated_at_ms>=? AND generated_at_ms<=? ORDER BY id DESC LIMIT 301", (chart_start, chart_end)).fetchall() if "scans" in names and not use_minutes else []
                truncated = len(snapshots) > 300
                for stamp, payload in reversed(snapshots[:300]):
                    rows = json.loads(payload).get("rows", [])
                    row = next((row for row in rows if row["id"] == identity), None)
                    if not row:
                        continue  # Gaps are not filled with invented observations.
                    reverse = next((other for other in rows if other["asset"] == row["asset"]
                        and other["long_exchange"] == row["short_exchange"] and other["short_exchange"] == row["long_exchange"]), None)
                    points.append(dict(time=stamp, entry=row["open_spread_pct"], exit=reverse["open_spread_pct"] if reverse else None,
                        long_price=row["long_ask_usdt_per_unit"], short_price=row["short_bid_usdt_per_unit"]))
                if "economics" in names and not use_minutes:
                    for stamp, payload in reversed(db.execute("SELECT generated_at_ms,payload FROM economics WHERE generated_at_ms>=? AND generated_at_ms<=? ORDER BY id DESC LIMIT 300", (chart_start, chart_end)).fetchall()):
                        snapshot = json.loads(payload)
                        for side, leg in item["legs"].items():
                            funding = next((value for value in snapshot.get("funding_observations", [])
                                if value["exchange"] == leg["exchange"] and value["native_id"] == leg["native_id"]), None)
                            if funding and number(funding.get("rate")) is not None and 0 <= stamp-funding["received_at_ms"] <= 120000:
                                observed_funding[side].append(dict(time=stamp, value=float(funding["rate"])*100))
                if use_minutes:
                    from .chart_history import read, pending
                    history_source, history_pending = "minute", pending(db)
                    points = read(db, 'pair:'+identity, chart_start, chart_end)
                    observed_funding = {side: read(db, 'fund:'+leg['exchange']+':'+leg['native_id'], chart_start, chart_end)
                                        for side, leg in item['legs'].items()}
                    truncated = False
                if "funding_history_events" in names:
                    for side, leg in item["legs"].items():
                        values = db.execute("SELECT timestamp_ms,rate FROM funding_history_events WHERE exchange=? AND native_id=? AND timestamp_ms>? AND timestamp_ms<=? ORDER BY timestamp_ms DESC LIMIT 1001",
                                            (leg["exchange"], leg["native_id"], max(0,chart_end-30*86400000), chart_end)).fetchall()
                        funding_series.append(dict(side=side, source="settled_public_history", truncated=len(values)>1000, rows=[dict(time=stamp, rate_pct=float(rate)*100) for stamp, rate in reversed(values[:1000])]))
                        first = db.execute('SELECT min(timestamp_ms) FROM funding_history_events WHERE exchange=? AND native_id=?', (leg['exchange'], leg['native_id'])).fetchone()[0]
                        if first is not None:
                            funding_earliest = min(funding_earliest, first) if funding_earliest is not None else first
                else:
                    truncated = bool(truncated)
            try:
                events = read_events(self.database, identity, 40)
            except sqlite3.Error:
                pass
        else:
            truncated = False
        return dict(item=item, points=points, funding_series=funding_series, observed_funding=observed_funding, events=events,
                    funding_comparison=comparison,
                    chart_range=dict(start_ms=chart_start, end_ms=chart_end, latest_ms=end, earliest_ms=earliest),
                    funding_range=dict(start_ms=max(0,chart_end-30*86400000), end_ms=chart_end, latest_ms=end, earliest_ms=funding_earliest),
                    alternatives=[dict(id=row['id'], long_exchange=row['legs']['long']['exchange'], short_exchange=row['legs']['short']['exchange'])
                                  for row in state['rows'] if row['asset'] == item['asset']],
                    mode=state["mode"], truncated=truncated, max_snapshots=300 if history_source == 'raw' else None,
                    history_source=history_source, history_pending=history_pending, window_minutes=minutes,
                    generated_at_ms=state["generated_at_ms"], execution_verified=False)


def create_app(snapshot, database, registry=None):
    store = Dashboard(snapshot, database, registry)
    @web.middleware
    async def headers(request, handler):
        response = await handler(request)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"})
        return response
    app = web.Application(middlewares=[headers])
    async def home(request):
        return web.FileResponse(WEB/"index.html")
    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.js", "chart-model.js", "style.css"}:
            raise web.HTTPNotFound()
        return web.FileResponse(WEB/name)
    async def state(request):
        return web.json_response(await asyncio.to_thread(store.state), dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False))
    async def alerts(request):
        from .alerts import read_feed
        try:
            limit=int(request.query.get('limit','50'))
            cursor=int(request.query['after_id']) if 'after_id' in request.query else None
            before=int(request.query['before_id']) if 'before_id' in request.query else None
            read_through=int(request.query.get('read_through','0'))
            values=await asyncio.to_thread(read_feed,store.database,limit,cursor,before,read_through)
        except ValueError:
            raise web.HTTPBadRequest(text='Неверный лимит или курсор')
        except sqlite3.Error:
            raise web.HTTPServiceUnavailable(text='Журнал временно недоступен')
        return web.json_response(values)
    async def pair(request):
        try:
            minutes = int(request.query.get("minutes", "15"))
            days = int(request.query.get("days", "7"))
            end_ms = int(request.query["end_ms"]) if "end_ms" in request.query else None
            if minutes not in (1, 15, 60, 360, 1440):
                raise ValueError("Unsupported interval")
            if days not in (1, 3, 7, 14, 28, 30):
                raise ValueError("Unsupported funding window")
            result = await asyncio.to_thread(store.pair, request.query.get("id", ""), minutes, days, end_ms)
        except KeyError:
            raise web.HTTPNotFound(text="Связка недоступна")
        except ValueError:
            raise web.HTTPBadRequest(text="Неверный интервал")
        return web.json_response(result, dumps=lambda x: json.dumps(x, ensure_ascii=False, allow_nan=False))
    app.router.add_get("/", home)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_get("/api/state", state)
    app.router.add_get('/api/alerts',alerts)
    app.router.add_get("/api/pair", pair)
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, default=Path("data/latest.json"))
    parser.add_argument("--database", type=Path, default=Path("data/market.sqlite3"))
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--registry", type=Path)
    parser.add_argument("--open-browser", action="store_true")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Port must be 1024–65535")
    try:
        registry = Registry.load(args.registry)
    except (ValueError, OSError, KeyError) as exc:
        parser.error(f"Invalid registry: {exc}")
    app = create_app(args.snapshot, args.database, registry)
    if args.open_browser:
        async def open_browser(app):
            asyncio.get_running_loop().call_later(0.5, webbrowser.open, f"http://127.0.0.1:{args.port}")
        app.on_startup.append(open_browser)
    web.run_app(app, host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()
