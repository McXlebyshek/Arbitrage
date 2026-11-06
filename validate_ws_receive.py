"""Synthetic scheduler benchmark, not a measurement of exchange latency."""
import argparse
import asyncio
import json
from pathlib import Path
import time
from arbitrage.atomic_json import write_json


async def check(frames):
    class BufferedSocket:
        async def receive(self): return 1
    socket=BufferedSocket()
    async def trial(use_wait_for):
        started=time.perf_counter()
        for index in range(frames):
            if use_wait_for: value=await asyncio.wait_for(socket.receive(),5)
            else:
                if index and index%256==0: await asyncio.sleep(0)
                async with asyncio.timeout(5): value=await socket.receive()
            assert value==1
        return (time.perf_counter()-started)*1000
    # Reverse order in the second round to expose warmup/order sensitivity.
    before,after=[],[]
    for order in ((True,False),(False,True)):
        for old in order: (before if old else after).append(await trial(old))
    return dict(classification='SYNTHETIC_BUFFERED_RECEIVE_SCHEDULER_NOT_NETWORK_LATENCY',
        frames_per_trial=frames,wait_for_ms=before,inline_timeout_ms=after,
        ratio_min_wait_for_to_max_inline=min(before)/max(after),
        interpretation='Only wrapper/scheduler overhead. Python 3.14 wait_for with positive timeout already awaits inline; no Task-per-frame conclusion.')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if args.output.exists(): parser.error('Output already exists')
    result=asyncio.run(check(50000))
    write_json(args.output,result,indent=2)
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
