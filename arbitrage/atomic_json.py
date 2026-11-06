"""Atomic JSON replacement with bounded retries for Windows reader sharing locks."""
import json
from pathlib import Path
import time
from uuid import uuid4


def write_json(path,value,indent=None):
    content=json.dumps(value,ensure_ascii=False,allow_nan=False,indent=indent)
    path=Path(path)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    created=False
    try:
        with temporary.open('x',encoding='utf-8') as handle:
            created=True
            handle.write(content)
        delays=(.01,.02,.04,.08,.16,.2,.2)
        for attempt in range(len(delays)+1):
            try:
                temporary.replace(path)
                return
            except PermissionError:
                if attempt==len(delays): raise
                time.sleep(delays[attempt])
    finally:
        # Only this call's uniquely created temporary file is eligible for cleanup.
        if created:
            try: temporary.unlink(missing_ok=True)
            except OSError: pass
