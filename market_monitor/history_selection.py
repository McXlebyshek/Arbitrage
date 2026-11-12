"""Select observations for chart history."""

from .market import compatible_assets, fresh


WATCH_TTL_MS = 120_000


def history_legs(markets, now, watched_keys, watched_groups):
    """Keep legs with comparable prices and those tracked by the browser."""
    selected = []
    for group, legs in markets.items():
        if group in watched_groups:
            selected.extend(legs)
            continue
        quoted = [
            leg for leg in legs if leg['bid'] and leg['ask'] and 0 < leg['bid'] <= leg['ask']
            and fresh(leg['receivedAt'], now)
            and (leg['sourceAt'] is None or fresh(leg['sourceAt'], now))
        ]
        used = set()
        for i, left in enumerate(quoted):
            for right in quoted[i + 1:]:
                if (left['quote'] != right['quote'] or left['type'] == right['type'] == 'spot'
                        or (left['exchange'] == right['exchange'] and left['type'] == right['type'])
                        or not compatible_assets(left, right)
                        or max(left['bid'], right['bid']) > 2 * min(left['bid'], right['bid'])):
                    continue
                a, b = left['sourceAt'], right['sourceAt']
                if a is not None and b is not None:
                    comparable = abs(a - b) <= 1500
                else:
                    comparable = abs(left['receivedAt'] - right['receivedAt']) <= 3000
                if comparable:
                    used.update((left['id'], right['id']))
            if len(used) == len(quoted):
                break
        selected.extend(leg for leg in legs if leg['id'] in used or leg['id'] in watched_keys)
    return selected
