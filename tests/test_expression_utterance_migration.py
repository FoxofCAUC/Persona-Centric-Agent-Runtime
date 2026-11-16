"""结构32与真实旧31程序的离线读写、再升级；无模型调用。"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys
import unittest

from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store
from persona_runtime.models import identifier
from tests import test_expression_runtime as fixture


class Migration(unittest.IsolatedAsyncioTestCase):
    setUp=fixture.Runtime.setUp
    asyncTearDown=fixture.Runtime.asyncTearDown
    submit=fixture.Runtime.submit
    acknowledge=fixture.Runtime.acknowledge
    all_visible=fixture.Runtime.all_visible

    @unittest.skipUnless(os.environ.get('PCAR_RC6_SOURCE'),'provide preserved rc6 source for actual old-program check')
    async def test_old31_writes_new_business_then_upgrade32_preserves_receipts_and_original(self):
        from persona_runtime.expression_persona_draft import PROFILE
        from persona_runtime.recall_rollback import prepare_snapshot
        self.runtime.configure(self.pid,{'protocol':PROFILE},self.runtime.policy(self.pid)['revision'],identifier())
        self.gate.raw_override=json.dumps({'body':self.gate.body,'segments':self.gate.parts},ensure_ascii=False)
        turn=await self.submit();self.all_visible(turn)
        with self.store.connection() as db:
            original=[tuple(r) for r in db.execute('SELECT * FROM expression_main_receipts')]
            raw=[tuple(r) for r in db.execute('SELECT id,raw_response,raw_response_hash,usage_json FROM expression_runs')]
            units=[tuple(r) for r in db.execute('SELECT id,source_id,source_hash,body,body_hash FROM expression_speech_units')]
        snapshot=prepare_snapshot(self.paths,target_schema=31)
        copy_home=self.paths.home/'compatible-31';paths=RuntimePaths(copy_home);paths.initialize()
        target=Store(paths).path;shutil.copyfile(snapshot['path'],target)
        code='''import json,sys
from pathlib import Path
from persona_runtime.paths import RuntimePaths
from persona_runtime.storage import Store,SCHEMA_VERSION
from persona_runtime.service import RuntimeService
p=RuntimePaths(Path(sys.argv[1]));s=Store(p);s.initialize()
assert SCHEMA_VERSION==31
event=RuntimeService(s).append_event(sys.argv[2],'note','旧rc6追加的验收业务')
print(json.dumps({'schema':SCHEMA_VERSION,'event_id':event['id']}))
'''
        env=dict(os.environ);env['PYTHONPATH']=os.environ['PCAR_RC6_SOURCE']
        proc=await asyncio.create_subprocess_exec(sys.executable,'-B','-X','utf8','-c',code,str(copy_home),turn['session_id'],
            cwd=os.environ['PCAR_RC6_SOURCE'],env=env,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        out,err=await proc.communicate();self.assertEqual(proc.returncode,0,err.decode('utf-8'));old=json.loads(out)
        self.assertEqual(old['schema'],31)
        copy=Store(paths);copy.initialize()
        with copy.connection() as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0],32)
            self.assertEqual([tuple(r) for r in db.execute('SELECT * FROM expression_main_receipts')],original)
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,raw_response,raw_response_hash,usage_json FROM expression_runs')],raw)
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,source_id,source_hash,body,body_hash FROM expression_speech_units')],units)
            self.assertEqual(db.execute('SELECT text FROM events WHERE id=?',(old['event_id'],)).fetchone()[0],'旧rc6追加的验收业务')
            self.assertFalse(json.loads(db.execute('SELECT data_json FROM expression_runtime_policies').fetchone()[0])['enabled'])
        with self.store.connection() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM events WHERE id=?',(old['event_id'],)).fetchone())


if __name__=='__main__':unittest.main()
