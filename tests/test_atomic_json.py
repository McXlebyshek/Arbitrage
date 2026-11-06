import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from arbitrage.atomic_json import write_json


class AtomicJSONTests(unittest.TestCase):
    def setUp(self):
        self.temp=TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'status.json'
        self.path.write_text('{"old":true}',encoding='utf-8')

    def test_transient_reader_denial_retries_and_preserves_old_file_until_success(self):
        replace=Path.replace
        seen=[]
        def locked(path,target):
            seen.append(json.loads(self.path.read_text(encoding='utf-8')))
            if len(seen)<4: raise PermissionError('sharing lock')
            return replace(path,target)
        with patch.object(Path,'replace',locked),patch('arbitrage.atomic_json.time.sleep') as sleep:
            write_json(self.path,dict(new='данные'))
        self.assertEqual(seen,[dict(old=True)]*4)
        self.assertEqual(sleep.call_count,3)
        self.assertEqual(json.loads(self.path.read_text(encoding='utf-8')),dict(new='данные'))
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_persistent_denial_is_bounded_and_old_status_survives(self):
        with patch.object(Path,'replace',side_effect=PermissionError('denied')) as replace,patch('arbitrage.atomic_json.time.sleep') as sleep:
            with self.assertRaises(PermissionError): write_json(self.path,dict(new=True))
        self.assertEqual(replace.call_count,8)
        self.assertLess(sum(call.args[0] for call in sleep.call_args_list),1)
        self.assertEqual(json.loads(self.path.read_text()),dict(old=True))
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_other_io_error_is_not_retried(self):
        with patch.object(Path,'replace',side_effect=OSError('disk failure')) as replace,patch('arbitrage.atomic_json.time.sleep') as sleep:
            with self.assertRaises(OSError): write_json(self.path,dict(new=True))
        self.assertEqual(replace.call_count,1)
        sleep.assert_not_called()
        self.assertEqual(json.loads(self.path.read_text()),dict(old=True))

    def test_nonfinite_serialization_does_not_create_temp_or_change_status(self):
        with self.assertRaises(ValueError): write_json(self.path,dict(value=float('nan')))
        self.assertEqual(json.loads(self.path.read_text()),dict(old=True))
        self.assertEqual(list(self.path.parent.glob('*.tmp')),[])

    def test_collision_never_removes_an_unowned_temp_file(self):
        collision=self.path.with_name(self.path.name+'.fixed.tmp')
        collision.write_text('unrelated',encoding='utf-8')
        with patch('arbitrage.atomic_json.uuid4',return_value=SimpleNamespace(hex='fixed')):
            with self.assertRaises(FileExistsError): write_json(self.path,dict(new=True))
        self.assertEqual(collision.read_text(),'unrelated')

    @unittest.skipUnless(os.name=='nt','Windows sharing-lock regression')
    def test_real_windows_reader_lock_releases_before_retry_budget(self):
        errors=[]
        def writing():
            try: write_json(self.path,dict(updated=True))
            except Exception as exc: errors.append(exc)
        with self.path.open('r',encoding='utf-8'):
            worker=threading.Thread(target=writing)
            worker.start()
            time.sleep(.06)
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertEqual(errors,[])
        self.assertEqual(json.loads(self.path.read_text()),dict(updated=True))
