"""Durable local notices for confirmed conditional scenarios; no external delivery."""
import argparse
from contextlib import closing
from dataclasses import dataclass
import json
import math
from pathlib import Path
import sqlite3
import time


@dataclass(frozen=True)
class AlertPolicy:
    enabled: bool = True
    cooldown_seconds: float = 60

    def __post_init__(self):
        if not math.isfinite(self.cooldown_seconds) or not 0 <= self.cooldown_seconds <= 86400:
            raise ValueError('Alert cooldown must be 0–86400 seconds')


def persist(db, signals, policy=None, now_ms=None):
    policy = policy or AlertPolicy()
    if not policy.enabled:
        return []
    now = int(time.time()*1000) if now_ms is None else now_ms
    db.execute('CREATE TABLE IF NOT EXISTS local_alerts(id INTEGER PRIMARY KEY AUTOINCREMENT,event_id TEXT NOT NULL UNIQUE,timestamp_ms INTEGER NOT NULL,pair_id TEXT NOT NULL,episode_id TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS local_alerts_pair_time ON local_alerts(pair_id,timestamp_ms)')
    db.execute('CREATE INDEX IF NOT EXISTS local_alerts_episode ON local_alerts(episode_id,id)')
    db.execute('CREATE INDEX IF NOT EXISTS local_alerts_time ON local_alerts(timestamp_ms)')
    db.execute("CREATE INDEX IF NOT EXISTS local_alerts_visible ON local_alerts(id) WHERE kind!='suppressed'")
    db.execute("CREATE INDEX IF NOT EXISTS local_alerts_opened_pair ON local_alerts(pair_id,timestamp_ms) WHERE kind='opened'")
    rows = {row['id']:row for row in signals.get('rows',[])}
    delivered = []
    for event in signals.get('recovered_events',[])+signals.get('events',[]):
        source_kind = event['kind']
        if source_kind not in ('confirmed','lost','ended','continuity_reset'):
            continue
        if db.execute('SELECT 1 FROM local_alerts WHERE event_id=?',(event['event_id'],)).fetchone():
            continue
        last = db.execute("SELECT kind FROM local_alerts WHERE episode_id=? AND kind IN ('opened','closed') ORDER BY id DESC LIMIT 1",(event['episode_id'],)).fetchone()
        kind, reason = None, None
        if source_kind == 'confirmed':
            row = rows.get(event['pair_id'])
            net = event.get('conditional_net_usdt')
            sources = (row or {}).get('last_sources') or {}
            valid = (row and row.get('state')=='confirmed' and row.get('episode_id')==event['episode_id']
                     and signals.get('state')=='monitoring' and event.get('to_state')=='confirmed'
                     and event['timestamp_ms']==signals.get('generated_at_ms')
                     and row.get('confirmations',0)>=signals.get('policy',{}).get('confirmations',3)
                     and event.get('confirmations',0)>=signals.get('policy',{}).get('confirmations',3)
                     and net is not None and math.isfinite(net) and net>max(0,signals.get('policy',{}).get('min_usdt',0))
                     and row.get('conditional_net_usdt')==net and len(sources)==2
                     and all(-1000<=now-stamp<=3000 for stamp in sources.values())
                     and -1000<=now-event['timestamp_ms']<=5000)
            recent = db.execute("SELECT max(timestamp_ms) FROM local_alerts WHERE pair_id=? AND kind='opened'",(event['pair_id'],)).fetchone()[0]
            if not valid:
                kind, reason = 'suppressed', 'not_current_confirmed_scenario'
            elif last and last[0]=='opened':
                kind, reason = 'suppressed', 'episode_already_notified'
            elif recent is not None and event['timestamp_ms']-recent<policy.cooldown_seconds*1000:
                kind, reason = 'suppressed', 'pair_cooldown'
            else:
                kind = 'opened'
        elif last and last[0]=='opened':
            kind = 'closed'
        if kind is None:
            continue
        item = dict(event, source_kind=source_kind, kind=kind, suppression_reason=reason,
                    classification='conditional_scenario_notice_not_execution', execution_verified=False,
                    delivery='local_journal', recorded_at_ms=now, confirmation_policy=signals.get('policy'),
                    cooldown_seconds=policy.cooldown_seconds)
        cursor = db.execute('INSERT INTO local_alerts(event_id,timestamp_ms,pair_id,episode_id,kind,payload) VALUES (?,?,?,?,?,?)',
                    (event['event_id'],event['timestamp_ms'],event['pair_id'],event['episode_id'],kind,json.dumps(item,ensure_ascii=False,allow_nan=False)))
        if kind != 'suppressed':
            delivered.append(dict(item, id=cursor.lastrowid))
    return delivered


def read_alerts(database, limit=50, after_id=None):
    if not 1<=limit<=1000 or (after_id is not None and (not isinstance(after_id,int) or after_id<0)):
        raise ValueError('Invalid alert limit or cursor')
    path=Path(database)
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='local_alerts' AND type='table'").fetchone():
            return []
        if after_id is None:
            rows=db.execute("SELECT id,payload FROM local_alerts WHERE kind!='suppressed' ORDER BY id DESC LIMIT ?",(limit,)).fetchall()[::-1]
        else:
            rows=db.execute("SELECT id,payload FROM local_alerts WHERE kind!='suppressed' AND id>? ORDER BY id LIMIT ?",(after_id,limit)).fetchall()
        return [dict(json.loads(payload),id=identity) for identity,payload in rows]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,default=Path('data/market.sqlite3'))
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--watch',action='store_true')
    args=parser.parse_args()
    try:
        rows=read_alerts(args.database,args.limit)
        print(json.dumps(dict(mode='saved_journal',rows=rows),ensure_ascii=False,indent=2),flush=True)
        cursor=max((row['id'] for row in rows),default=0)
        while args.watch:
            time.sleep(2)
            batch=read_alerts(args.database,1000,cursor)
            for row in batch:
                print(json.dumps(row,ensure_ascii=False),flush=True)
                cursor=row['id']
    except KeyboardInterrupt:
        pass
    except (ValueError,sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__=='__main__':
    main()
