"""Read-only sampled freshness windows; never infers continuous uptime."""
import argparse
from collections import defaultdict
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3

from arbitrage.atomic_json import write_json


def summarize(database,report,window_seconds=60):
    if type(window_seconds) is not int or not 1<=window_seconds<=3600:raise ValueError('invalid_window_seconds')
    database=Path(database).resolve()
    if not database.is_file():raise ValueError('source_database_missing')
    started=report['started_at_ms'];warmup=report['warmup_seconds']
    if (type(started) is not int or isinstance(warmup,bool) or not isinstance(warmup,(int,float))
            or not math.isfinite(warmup) or warmup<0):raise ValueError('invalid_report_clock')
    buckets=defaultdict(dict)
    with closing(sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)) as db:
        for stamp,exchange,payload in db.execute('SELECT collected_at_ms,exchange,payload FROM snapshots WHERE collected_at_ms>=? ORDER BY collected_at_ms,id',(started+warmup*1000,)):
            if report.get('finished_at_ms') is not None and stamp>report['finished_at_ms']:continue
            status=json.loads(payload)['status']
            if status['state']=='stopped':continue
            index=(stamp-started)//(window_seconds*1000)
            row=buckets[exchange].setdefault(index,dict(start_seconds=index*window_seconds,end_seconds=(index+1)*window_seconds,
                samples=0,selected_sum=0,fresh_sum=0,streaming_samples=0,first_sample_ms=stamp,last_sample_ms=stamp))
            selected,fresh=status['markets'],status['usable_quotes']
            if type(selected) is not int or type(fresh) is not int or not 0<=fresh<=selected:raise ValueError('invalid_coverage_counts')
            row['samples']+=1;row['selected_sum']+=selected;row['fresh_sum']+=fresh
            row['streaming_samples']+=status['state']=='streaming';row['last_sample_ms']=stamp
    exchanges={}
    for exchange,stats in report['exchanges'].items():
        peak=stats['max_selected_markets']
        if type(peak) is not int or peak<0:raise ValueError('invalid_report_peak')
        rows=[buckets[exchange][key] for key in sorted(buckets[exchange])]
        for row in rows:
            if row['selected_sum']>peak*row['samples']:raise ValueError('report_peak_does_not_cover_snapshots')
            row['fresh_fraction_of_selected']=row['fresh_sum']/row['selected_sum'] if row['selected_sum'] else None
            row['fresh_fraction_of_peak_capacity']=row['fresh_sum']/(row['samples']*peak) if peak else None
            row['streaming_fraction']=row['streaming_samples']/row['samples']
            row['complete_time_window']=bool(report.get('finished_at_ms') is not None
                and started+row['start_seconds']*1000>=started+warmup*1000
                and started+row['end_seconds']*1000<=report['finished_at_ms'])
        fractions=[row['fresh_fraction_of_peak_capacity'] for row in rows if row['fresh_fraction_of_peak_capacity'] is not None]
        complete=[row['fresh_fraction_of_peak_capacity'] for row in rows if row['complete_time_window'] and row['fresh_fraction_of_peak_capacity'] is not None]
        exchanges[exchange]=dict(peak_selected_markets=peak,windows=rows,
            min_window_fresh_fraction_of_peak_capacity=min(fractions) if fractions else None,
            min_complete_window_fresh_fraction_of_peak_capacity=min(complete) if complete else None)
    return dict(classification='READ_ONLY_SAMPLED_FRESHNESS_WINDOWS',run_id=report.get('run_id'),
        source_finished=bool(report.get('finished')),window_seconds=window_seconds,warmup_seconds=warmup,exchanges=exchanges,
        interpretation='Snapshots after warmup, stopped excluded. Ratios use selected counts or observed peak capacity; not continuous uptime. Empty windows omitted, no-data/zero-capacity is null.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database',type=Path,required=True)
    parser.add_argument('--run-report',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--window-seconds',type=int,default=60)
    args=parser.parse_args()
    if args.output.suffix.lower()!='.json' or args.output.exists():parser.error('Output must be a new JSON file')
    if args.output.resolve() in (args.database.resolve(),args.run_report.resolve()):parser.error('Output must differ from inputs')
    result=summarize(args.database,json.loads(args.run_report.read_text(encoding='utf-8')),args.window_seconds)
    write_json(args.output,result,indent=2)
    print(json.dumps({key:value['min_complete_window_fresh_fraction_of_peak_capacity'] for key,value in result['exchanges'].items()},indent=2))


if __name__=='__main__':main()
