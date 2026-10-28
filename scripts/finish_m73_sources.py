"""Zero-call R07 source checks on the explicitly selected M7.3 test persona."""
import argparse
import json
from pathlib import Path
from scripts.acceptance_m73 import Acceptance
from persona_runtime.config import atomic_json
from persona_runtime.desktop_client import LocalAPIError


def run(output):
    driver=Acceptance(output);driver.online();state=driver.state;out=driver.out/'R07-api.json'
    if out.exists():raise RuntimeError('R07 already recorded; inspect it, do not repeat')
    if state['calls'].get('R06-desktop',{}).get('status')!='completed':raise RuntimeError('Desktop organization must complete first')
    api=driver.client.manage;scope=driver.scope();sid=state['desktop_seed'];count=driver.count_calls()
    try:
        driver.use(driver.pid)
        before=api('session.package',**scope,session_id=sid)
        segment=api('topic.create',**scope,title='人工书店种子',description='只组织展示，不是新证据',session_id=sid,turn_ids=[state['seed_turn']],expected_revision=0,operation_id='m73-r07-topic')
        second=api('topic.create',**scope,title='人工接续及引用',description='第二个封存会话',session_id=state['continued_session'],turn_ids=[state['calls'][s]['turn_id'] for s in ('R02','R03')],expected_revision=0,operation_id='m73-r07-topic2')
        thread=api('thread.create',**scope,title='人工跨会话整理',description='同入口主题关联，自动跨会话仍关闭',expected_revision=0,operation_id='m73-r07-thread')
        link=api('thread.link',**scope,thread_id=thread['id'],segment_id=segment['id'],expected_revision=thread['revision'],segment_revision=segment['revision'],operation_id='m73-r07-link')
        api('thread.link',**scope,thread_id=thread['id'],segment_id=second['id'],expected_revision=link['thread_revision'],segment_revision=second['revision'],operation_id='m73-r07-link2')
        policy=api('interaction.current',**scope);assert not policy['cross_session']
        try:api('session.replay',**(scope|{'persona_id':driver.original}),session_id=sid)
        except LocalAPIError as error:denied=str(error)
        else:raise AssertionError('Cross-persona replay unexpectedly accessible')
        r01=json.loads((driver.out/'R01.json').read_text(encoding='utf-8'))['turn'];member=r01['input_members'][1]
        memory=api('memory.add',category='shared_experience',text='验收用虚构书店预算三百元',source_event_id=member['id'])
        atomic_json(driver.out/'R07-withdraw-intent.json',{'memory_id':memory['id'],'member_id':member['id'],'turn_id':r01['id']})
        api('memory.retract',memory_id=memory['id'])
        current=api('topic.show',**scope,object_id=segment['id']);assert not current['usable']
        replay=api('session.replay',**scope,session_id=sid);assert not replay['groups'][0]['usable']
        preview=api('session.organization.preview',**scope,session_id=sid);assert preview['status']=='no_action' and preview['excluded']
        after=api('session.package',**scope,session_id=sid);assert before==after
        summaries=api('summary.list',session_id=sid);assert summaries and all(s['status']!='active' for s in summaries)
        candidates=[c for c in api('candidate.list') if c['task_id']==state['calls']['R06-desktop']['task_id']]
        assert all(c['status']=='stale' for c in candidates)
        thread=api('thread.show',**scope,object_id=thread['id']);assert not thread['affects_recall'] and len(thread['segment_ids'])==2
        assert count==driver.count_calls()
        atomic_json(out,{'passed':True,'interface':'actual_local_admin_API','native_gui':'tracked_separately','model_calls':0,'package_unchanged':True,'cross_persona_denied':denied,'topic':current,'thread':thread,'preview':preview,'summary_statuses':[s['status'] for s in summaries],'candidate_statuses':[c['status'] for c in candidates],'nonfirst_member_retracted':member['id'],'cross_session':policy['cross_session']})
        print(json.dumps({'passed':True,'additional_calls':0,'source_retracted':True,'package_unchanged':True}))
    finally:driver.use(driver.original)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);run(p.parse_args().output)
