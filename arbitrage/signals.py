"""Session-local confirmation of conditional scenarios; never verifies execution."""
from copy import deepcopy
from uuid import uuid4

from .scanner import decimal


class SignalTracker:
    def __init__(self, confirmations=3, min_seconds=2, max_gap_seconds=10, min_usdt=0, max_skew_ms=1000):
        if not 2 <= confirmations <= 20 or not 0 <= min_seconds <= max_gap_seconds or max_gap_seconds <= 0 or decimal(min_usdt) < 0:
            raise ValueError("Invalid confirmation policy")
        self.policy = dict(confirmations=confirmations, min_seconds=min_seconds,
                           max_gap_seconds=max_gap_seconds, min_usdt=float(min_usdt), max_skew_ms=max_skew_ms)
        self.run_id, self.active, self.last_timestamp = str(uuid4()), {}, None
        self.sequence = 0

    def update(self, economics, scanner, timestamp):
        events = []
        def event(kind, item, previous, reasons):
            self.sequence += 1
            events.append(dict(event_id=f"{self.run_id}:{self.sequence}", run_id=self.run_id,
                episode_id=item["episode_id"], pair_id=item["id"], timestamp_ms=timestamp,
                kind=kind, from_state=previous, to_state=item["state"], reasons=list(reasons),
                confirmations=item["confirmations"], conditional_net_usdt=item.get("conditional_net_usdt")))

        stopped = economics is None or economics.get("state") == "stopped"
        clock_bad = self.last_timestamp is not None and timestamp <= self.last_timestamp
        rows = [] if stopped or clock_bad else economics.get("rows", [])
        present = {row["id"] for row in rows}
        available = {row["id"] for row in scanner.get("rows", [])}
        ended = []
        for key in sorted(set(self.active)-present):
            item = self.active.pop(key)
            previous = item["state"]
            item.update(state="ended", confirmations=0, updated_at_ms=timestamp,
                reasons=["monitor_stopped" if stopped else "publication_clock_not_advancing" if clock_bad
                         else "selection_changed" if key in available else "quote_unavailable_or_ineligible"])
            event("ended", item, previous, item["reasons"])
            ended.append(item)
        for row in rows:
            key = row["id"]
            item = self.active.get(key)
            new = item is None
            if new:
                item = dict(id=key, episode_id=str(uuid4()), run_id=self.run_id,
                    first_seen_at_ms=timestamp, state=None, confirmations=0,
                    streak_started_at_ms=None, last_counted_at_ms=None, last_sources=None)
            previous, old_reasons = item["state"], item.get("reasons", [])
            reasons = list(row.get("reasons", []))
            sources = row.get("depth_source_times")
            eligible = row.get("status") == "scenario_estimated" and not reasons
            value = row.get("convergence_net_scenario_usdt")
            try:
                if not eligible:
                    reasons = reasons or ["economic_scenario_incomplete"]
                elif value is None or decimal(value) <= decimal(self.policy["min_usdt"]):
                    eligible, reasons = False, ["conditional_net_not_above_threshold"]
                elif not sources or any(side not in sources or sources[side] is None for side in ("long", "short")):
                    eligible, reasons = False, ["depth_source_time_unknown"]
                elif any(not -1000 <= timestamp-sources[side] <= 3000 for side in ("long", "short")):
                    eligible, reasons = False, ["depth_source_time_ineligible"]
                elif abs(sources["long"]-sources["short"]) > self.policy["max_skew_ms"]:
                    eligible, reasons = False, ["depth_time_skew"]
            except (ValueError, TypeError, ArithmeticError):
                eligible, reasons = False, ["invalid_conditional_observation"]
            item.update(updated_at_ms=timestamp, conditional_net_usdt=value, economic_status=row.get("status"))
            gap = (item["last_counted_at_ms"] is not None
                   and timestamp-item["last_counted_at_ms"] > self.policy["max_gap_seconds"]*1000)
            if eligible and item["last_sources"] and any(sources[side] < item["last_sources"][side] for side in ("long", "short")):
                eligible, reasons = False, ["depth_source_time_regressed"]
            if not eligible:
                item.update(state="rejected", confirmations=0, streak_started_at_ms=None,
                            last_counted_at_ms=None, last_sources=None, reasons=reasons)
            else:
                if gap:
                    item.update(confirmations=0, streak_started_at_ms=None, last_sources=None)
                fresh_sample = item["last_sources"] is None or all(sources[side] > item["last_sources"][side] for side in ("long", "short"))
                if fresh_sample:
                    item["confirmations"] += 1
                    item["streak_started_at_ms"] = item["streak_started_at_ms"] if item["streak_started_at_ms"] is not None else timestamp
                    item.update(last_counted_at_ms=timestamp, last_sources=dict(sources))
                confirmed = (item["confirmations"] >= self.policy["confirmations"]
                    and item["last_counted_at_ms"]-item["streak_started_at_ms"] >= self.policy["min_seconds"]*1000)
                item.update(state="confirmed" if confirmed else "pending", reasons=[])
                if gap:
                    event("continuity_reset", item, previous, ["confirmation_gap_exceeded"])
            if new:
                event("appeared", item, None, item["reasons"])
            elif previous != item["state"] and not (eligible and gap):
                event("confirmed" if item["state"] == "confirmed" else "lost" if item["state"] == "rejected" else "qualifying",
                      item, previous, item["reasons"])
            elif old_reasons != item["reasons"] and not (eligible and gap):
                event("reason_changed", item, previous, item["reasons"])
            self.active[key] = item
        self.last_timestamp = timestamp
        return deepcopy(dict(run_id=self.run_id, generated_at_ms=timestamp,
            state="stopped" if stopped else "reset" if clock_bad else "monitoring",
            policy=self.policy, rows=list(self.active.values()), ended=ended, events=events,
            classification="confirmed_conditional_scenario_not_execution", execution_verified=False))
