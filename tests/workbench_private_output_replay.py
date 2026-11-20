"""Replay a length-rejected research turn through a fresh, loopback-only Windows workbench."""
import argparse
from contextlib import closing
import ipaddress
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
from unittest.mock import patch

from persona_runtime.config import ConfigStore
from persona_runtime.expression_contract import text_hash
from persona_runtime.models import PersonaSpec
from persona_runtime.paths import RuntimePaths
from persona_runtime.private_reply_output import ADVISORY_VERSION
from persona_runtime.storage import Store
from persona_runtime.turn_input import load_turn_input
from tests.workbench_daily_cycle import wait
from tests.workbench_import_v2_smoke import Screen
from tests.workbench_private_daily_cycle import setup


def archive(path, tid):
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as db:
        db.row_factory = sqlite3.Row
        turn = dict(db.execute('SELECT * FROM turns WHERE id=?', (tid,)).fetchone())
        members = load_turn_input(db, turn)
        assert len(members)==1 and turn['error_code']=='private_reply_review_rejected'
        profile = json.loads(turn['profile_snapshot'])
        spec = PersonaSpec.from_dict(dict(format_version=1, **profile['core']['data'],
                                          expression_style=profile['expression']['data']['style']))
        decisions = {r['node']: json.loads(r['result_json']) for r in db.execute(
            """SELECT node,result_json FROM private_reply_nodes WHERE turn_id=? AND draft=0
               AND state='received' AND node NOT LIKE '%handoff'""", (tid,))}
        assert decisions['J1']==members[0]['id'] and decisions['J5']=='insufficient'
        bodies, styles = {}, {}
        for row in db.execute("""SELECT a.phase,a.body,a.body_sha256,r.approved,r.reason,d.decision_json
                FROM private_reply_attempts a JOIN private_reply_reviews r ON r.attempt_id=a.id
                JOIN private_reply_decisions d ON d.id=a.decision_id
                WHERE a.turn_id=? AND a.draft=0 AND a.state='received'""", (tid,)):
            phase = row['phase']
            assert text_hash(row['body'])==row['body_sha256']
            assert (row['approved'],row['reason'])==((1,'accept') if phase=='search_notice' else (0,'length'))
            assert decisions['notice.J7' if phase=='search_notice' else 'J7']=='accept'
            bodies[phase] = row['body']
            styles[decisions['notice.J2.style' if phase=='search_notice' else 'J2.style']] = json.loads(row['decision_json'])['style']
        assert set(bodies)=={'main','search_notice'} and bodies['main']!=bodies['search_notice']
        tool, = db.execute("SELECT * FROM private_lookup_calls WHERE turn_id=?", (tid,)).fetchall()
        assert tool['state']=='received' and tool['step']=='native'
        result=json.loads(tool['result_json'])
        it=db.execute('SELECT * FROM private_lookup_intents WHERE turn_id=?',(tid,)).fetchone()
        assert it['origin']=='derived'
        return spec, {'input':members[0]['text'], 'query':json.loads(it['query_json']), 'choices':decisions,
                      'bodies':bodies,'styles':styles,'tool':result,'original_tool_id':tool['attempt_id']}


def service(module):
    from persona_runtime import server
    from persona_runtime.private_lookup import visible_notice
    def loopback(event,args):
        if event=='socket.connect' and isinstance(args[1],tuple):
            address=args[1][0]
            if address!='localhost' and not ipaddress.ip_address(address).is_loopback:
                raise RuntimeError('offline workbench replay')
    sys.addaudithook(loopback)
    paths=RuntimePaths.from_environment()
    stored=json.loads((paths.home/'replay.private.json').read_bytes())
    identity=json.loads((paths.home/'cycle.json').read_bytes())
    store=Store(paths)
    class Provider:
        version='archived-research-replay'
        research=True
        output_contract_version=ADVISORY_VERSION
        styles={identity['persona']:stored['styles']}

        async def choose(self,node,aid,state,options):
            with store.connection() as db:
                key=db.execute('SELECT node FROM private_reply_nodes WHERE attempt_id=?',(aid,)).fetchone()[0]
            choice=stored['choices'][key]
            if node=='J1':
                member,=state['current']
                assert member['text']==stored['input']
                choice=member['id']
            assert choice in options
            return choice, {'replayed':True}

        async def reply(self,aid,packet):
            phase=packet['phase']
            assert packet['current'][0]['text']==stored['input']
            assert packet['mode']=='short_answer' and packet['output_constraints']['max_bubble_chars']==75
            if phase=='main':
                assert packet['research']['status']=='insufficient' and not packet['research']['facts']
            return stored['bodies'][phase], {'replayed':True}

        async def retrieve(self,step,aid,query,model):
            assert step=='native' and query==stored['query']
            with store.connection() as db:
                tid=db.execute('SELECT turn_id FROM private_lookup_calls WHERE attempt_id=?',(aid,)).fetchone()[0]
                assert visible_notice(db,tid)
            return stored['tool'], {'replayed_attempt':stored['original_tool_id']}

    create=server.create_app
    with patch.object(server,'create_app',lambda *a,**kw:create(*a,private_chat_providers=Provider(),**kw)):
        runpy.run_module(module,run_name='__main__')


def desktop(paths, phase):
    from persona_runtime import workbench_webview as gui
    from persona_runtime.app_processes import AppProcesses
    from persona_runtime.lifecycle import ServiceController, port_open
    data=json.loads((paths.home/'replay.private.json').read_bytes())
    sid=json.loads((paths.home/'cycle.json').read_bytes())['session']
    errors=[]
    result={}
    def exercise(host):
        try:
            screen=Screen(host.window)
            screen.wait('window.pcarLive?.active && !pcarLive.offline && currentSession()?.id==='+json.dumps(sid),timeout=50)
            if phase=='submit':
                screen.wait('!message.disabled && !pcarLive.selecting')
                screen.eval('message.value='+json.dumps(data['input'])+
                            ";message.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#composer').requestSubmit();true")
            expected=[data['bodies'][k] for k in ('search_notice','main')]
            bubbles="[...document.querySelectorAll('#timeline .bubble.reply')].filter(b=>b.getClientRects().length).map(b=>b.textContent)"
            screen.wait('JSON.stringify('+bubbles+')==='+json.dumps(json.dumps(expected,ensure_ascii=False,separators=(',',':'))),timeout=65)
            result['dom_body_hashes']=[text_hash(s) for s in screen.eval(bubbles)]
            store=Store(paths)
            def committed():
                with store.connection() as db:
                    return db.execute("SELECT id FROM turns WHERE session_id=? AND status='completed'",(sid,)).fetchone()
            tid=wait(committed)[0]
            with store.connection() as db:
                rows=db.execute("""SELECT u.phase,v.text,v.receipt_id,p.state,e.text canonical
                    FROM expression_speech_units u JOIN expression_speech_visible v ON v.utterance_id=u.id
                    JOIN expression_speech_publications p ON p.id=v.publication_id JOIN events e ON e.id=p.canonical_event_id
                    WHERE u.turn_id=? ORDER BY u.rowid,v.ordinal""",(tid,)).fetchall()
                assert [r['phase'] for r in rows]==['search_notice','main']
                assert [r['text'] for r in rows]==[r['canonical'] for r in rows]==expected
                assert all(r['state']=='complete' and r['receipt_id'] for r in rows)
                assert db.execute('SELECT count(*) FROM turns WHERE session_id=?',(sid,)).fetchone()[0]==1
                assert db.execute('SELECT count(*) FROM private_reply_attempts WHERE turn_id=?',(tid,)).fetchone()[0]==2
                audit,=db.execute('SELECT usage_json FROM private_lookup_calls WHERE turn_id=?',(tid,)).fetchall()
                assert json.loads(audit[0])['replayed_attempt']==data['original_tool_id']
                result.update(turn_id=tid,receipts=[r['receipt_id'] for r in rows],stored_tool_replays=1)
        except BaseException as exc:
            errors.append(exc)
        finally:
            host.controls.exit_workbench()
    class Host(gui.WebViewHost):
        def run(self,web,html):
            start_web=web.start
            with patch.object(web,'start',lambda **opts:start_web(lambda:exercise(self),**opts)):
                super().run(web,html)
    orig=AppProcesses.start
    def start(owner,role,args,**kw):
        assert role=='service' and args[3]=='persona_runtime.host'
        cmd=f'from tests.workbench_private_output_replay import service;service({args[3]!r})'
        return orig(owner,role,[args[0],'-B','-c',cmd,*args[4:]],**kw)
    with patch.object(AppProcesses,'start',start), patch.object(gui,'WebViewHost',Host):
        gui.run(paths)
    assert not ServiceController(paths).has_resident_processes() and not port_open(ConfigStore(paths).load()['service_port'])
    if errors:raise errors[0]
    (paths.home/(phase+'.private.json')).write_text(json.dumps(result),encoding='utf8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('database',type=Path)
    parser.add_argument('archived_turn')
    parser.add_argument('fresh_home',type=Path)
    args=parser.parse_args()
    home=args.fresh_home.resolve()
    spec,data=archive(args.database,args.archived_turn)
    setup(home,persona_spec=spec)
    (home/'replay.private.json').write_text(json.dumps(data,ensure_ascii=False),encoding='utf8')
    env=os.environ|{'PERSONA_RUNTIME_HOME':str(home),'PCAR_PYTHON':sys.executable,'PYTHONUTF8':'1'}
    for phase in ('submit','reopen'):
        cmd=f'from tests.workbench_private_output_replay import desktop,RuntimePaths;desktop(RuntimePaths.from_environment(),"{phase}")'
        subprocess.run([sys.executable,'-B','-c',cmd],env=env,check=True,timeout=135)
    first=json.loads((home/'submit.private.json').read_bytes())
    assert first==json.loads((home/'reopen.private.json').read_bytes())
    print(json.dumps({'visible_characters':[len(data['bodies'][k]) for k in ('search_notice','main')],
                      'same_receipts_after_restart':True,'external_calls':0,**first}))


if __name__=='__main__':
    main()
