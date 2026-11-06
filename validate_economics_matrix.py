"""Public six-venue profile probe and deterministic, offline replay. No orders or keys."""
import argparse
import asyncio
from collections import Counter, defaultdict
from itertools import permutations
import json
from pathlib import Path
from types import SimpleNamespace

from arbitrage.economic_data import EconomicData, now_ms
from arbitrage.economics import evaluate_pair
from arbitrage.live import RequestLimiter
from arbitrage.scanner import Registry
from arbitrage.validation import PROFILES, audit_result


def calculate(batch, registry):
    legs = {(leg["exchange"], leg["native_id"]): leg for leg in batch["legs"]}
    cases = []
    for first, second in permutations(batch["bindings"], 2):
        pair = dict(id=f'{batch["asset"]}|{first["exchange"]}:{first["id"]}|{second["exchange"]}:{second["id"]}',
                    asset=batch["asset"], long_exchange=first["exchange"], short_exchange=second["exchange"],
                    long_symbol=first["id"], short_symbol=second["id"])
        for profile in PROFILES:
            result = evaluate_pair(pair, legs, registry, batch["timestamp_ms"], **profile)
            result["validation_timestamp_ms"] = batch["timestamp_ms"]
            cases.append(dict(pair=pair, profile=profile, result=result,
                              audit_errors=audit_result(result, pair, legs, registry, profile)))
    return cases


def summary(document):
    # All attempts remain in the artifact. Summary uses the first most complete
    # result per pair/profile, and reports all-attempt counts separately.
    selected = {}
    priority = {"blocked": 0, "entry_checked": 1, "scenario_estimated": 2}
    all_cases = [case for batch in document["batches"] for case in batch["cases"]]
    for case in all_cases:
        key = (case["pair"]["id"], case["profile"]["capital"], case["profile"]["hold_hours"])
        if key not in selected or priority[case["result"]["status"]] > priority[selected[key]["result"]["status"]]:
            selected[key] = case
    groups = defaultdict(list)
    for case in selected.values():
        groups[case["pair"]["asset"]].append(case)
    def counts(cases):
        return {"cases": len(cases), "statuses": dict(Counter(case["result"]["status"] for case in cases)),
                "reasons": dict(Counter(reason for case in cases for reason in case["result"]["reasons"])),
                "audit_errors": sum(len(case["audit_errors"]) for case in cases)}
    return dict(unique_cases=counts(list(selected.values())), all_attempts=counts(all_cases),
                by_asset={asset: counts(cases) for asset, cases in sorted(groups.items())},
                by_profile={f'{profile["capital"]}USDT/{profile["hold_hours"]}h': counts([
                    case for case in selected.values() if case["profile"] == profile]) for profile in PROFILES},
                input_errors=document["input_errors"],
                selection="first most complete attempt per direction/profile; all attempts retained",
                conditional_only=True)


async def collect(target):
    registry_path = Path(__file__).parent/"arbitrage/instruments.json"
    registry_document = json.loads(registry_path.read_text(encoding="utf-8"))
    registry = Registry(registry_document)
    exchanges = sorted({key[0] for key in registry.bindings})
    pools = [SimpleNamespace(exchange=exchange, selected={}, snapshot_limiter=RequestLimiter()) for exchange in exchanges]
    args = SimpleNamespace(registry=registry)
    engine = EconomicData(args, pools)
    document = dict(schema_version=1, started_at_ms=now_ms(), registry=registry_document,
                    profiles=PROFILES, batches=[], input_errors=[], policy=dict(max_age_ms=3000, max_skew_ms=1000,
                    min_volume_usdt=1000000, max_impact=0.001, min_utilization=0.9, account_fees_verified=False))
    def save():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    async def invoke(method, entry):
        try:
            await method(entry["exchange"], [entry["id"]])
        except Exception as exc:
            document["input_errors"].append(f'{entry["exchange"]}/{entry["id"]}: {type(exc).__name__}: {exc}')
    async def prepare(exchange):
        try:
            client = await engine.client(exchange)
            natives = [key[1] for key in registry.bindings if key[0] == exchange]
            engine.pools[exchange].selected = {m["id"]: m for m in client.markets.values()
                if m["id"] in natives and m.get("swap") and m.get("linear") and m.get("settle") == "USDT"}
            await engine.enrich_funding(exchange, natives)
            print(f"prepared {exchange}: {len(engine.pools[exchange].selected)} markets", flush=True)
        except Exception as exc:
            document["input_errors"].append(f"prepare {exchange}: {type(exc).__name__}: {exc}")
    try:
        await asyncio.gather(*(prepare(exchange) for exchange in exchanges))
        for asset in sorted({binding["asset"] for binding in registry.bindings.values()}):
            bindings = [entry for entry in registry_document["bindings"] if entry["asset"] == asset]
            for attempt in range(2):
                # Refresh rates/volume before depth, so data ages are measured honestly.
                await asyncio.gather(*(invoke(engine.enrich_funding, entry) for entry in bindings))
                await asyncio.gather(*(invoke(engine.fetch_books, entry) for entry in bindings))
                timestamp = now_ms()
                batch = dict(asset=asset, attempt=attempt+1, timestamp_ms=timestamp, bindings=bindings,
                    legs=[dict(exchange=entry["exchange"], native_id=entry["id"], **engine.legs[(entry["exchange"], entry["id"])],
                               funding=engine.funding.get((entry["exchange"], entry["id"])),
                               liquidity=engine.liquidity.get((entry["exchange"], entry["id"])))
                          for entry in bindings if (entry["exchange"], entry["id"]) in engine.legs],
                    errors=list(engine.errors.values()))
                batch["cases"] = calculate(batch, registry)
                document["batches"].append(batch)
                save()
                print(asset, attempt+1, dict(Counter(case["result"]["status"] for case in batch["cases"])), flush=True)
        document["finished_at_ms"] = now_ms()
        document["input_errors"].extend(sorted(set(error for batch in document["batches"] for error in batch["errors"])))
        document["summary"] = summary(document)
        save()
        return document
    finally:
        await asyncio.gather(*(client.close() for client in engine.clients.values()), return_exceptions=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("data/economics-matrix.json"))
    parser.add_argument("--replay", type=Path)
    args = parser.parse_args()
    if args.replay:
        document = json.loads(args.replay.read_text(encoding="utf-8"))
        registry, mismatches = Registry(document["registry"]), 0
        for batch in document["batches"]:
            recalculated = calculate(batch, registry)
            mismatches += recalculated != batch["cases"]
        print(json.dumps(dict(replayed_batches=len(document["batches"]), mismatched_batches=mismatches)))
        if mismatches:
            raise SystemExit(1)
    else:
        document = asyncio.run(collect(args.output))
        print(json.dumps(document["summary"], ensure_ascii=False, indent=2))
        if document["summary"]["all_attempts"]["audit_errors"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
