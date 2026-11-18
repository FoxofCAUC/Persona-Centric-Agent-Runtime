"""Freeze exact human bubbles and their two public excerpts for the desktop experience."""
import argparse
from datetime import datetime, timedelta, timezone
from html import escape
import json
from pathlib import Path

from experiments.corpus_reply_study import read_private, save_once
from experiments.corpus_reply_trial_report import save_text_once
from experiments.en5_experience_setup import binding_at, use_scope
from experiments.fresh_scene_material import IDENTIFIER
from persona_runtime.corpus_dataset import DatasetCorpusStore
from persona_runtime.corpus_files import CorpusError, digest
from persona_runtime.corpus_store import outside_repository


VERSION='en5-original-inputs-v1'
ASSEMBLERS=('experiments/en5_experience_replay.py','experiments/en5_experience_material.py',
            'experiments/en5_experience_setup.py','persona_runtime/en5_display.py','persona_runtime/desktop.py',
            'persona_runtime/en5_replay_scope.py','persona_runtime/knowledge_native.py')


def require(test, detail):
    if not test: raise CorpusError('en5_material_'+detail)


def script_hashes():
    repo=Path(__file__).resolve().parents[1]
    return {name:digest((repo/name).read_bytes().replace(b'\r\n',b'\n')) for name in ASSEMBLERS}


def origin(row,view):
    m=json.loads(row['data_json'])
    text=m['text']
    require(row['id']==m['message_id'] and row['ordinal']==m['ordinal']
            and row['source_kind']==m['kind']=='text' and {'self':'A','peer':'B'}.get(row['source_role'])==m['role']
            and row['target_candidate']==1 and not m['flags'] and not m.get('redactions')
            and row['split']==m['split']==row['fragment_split']==row['group_split']
            and row['fragment_id']==m['fragment_id'] and not row['quoted']
            and row['original']==text and text and len(text)<=240 and not IDENTIFIER.search(text)
            and row['raw_hash']==m['source_text_hash']==row['text_hash']==digest(text.encode()), 'source_ineligible')
    return {'message_id':row['id'],'ordinal':row['ordinal'],'source_speaker':m['role'],'text':text,
            'text_hash':row['text_hash'],'split':m['split'],'group_id':row['group_id'],'fragment_id':row['fragment_id'],
            'source_time':datetime.fromtimestamp(row['created_epoch'],timezone(timedelta(hours=8))).isoformat()}


def build(root):
    root=outside_repository(root)
    selection=read_private(root/'selection.private.json')
    corpus=DatasetCorpusStore(selection['corpus_root'])
    require(len(selection['sessions'])==2,'session_count')
    collected=[]; seen=set()
    with corpus.connection() as db:
        db.execute('BEGIN')
        v=corpus._view_status(db,selection['view_id'])
        require(v['payload_hash']==selection['view_payload_hash'] and
                v['source_proof_hash']==selection['source_proof_hash'],'source_authority_changed')
        for part,part_spec in enumerate(selection['sessions'],1):
            ordinals=part_spec['ordinals']
            require(len(ordinals)==15 and ordinals==sorted(set(ordinals)) and not seen.intersection(ordinals),'turn_order')
            seen.update(ordinals)
            turns=[]
            for n,ordinal in enumerate(ordinals,1):
                row=db.execute("""SELECT m.data_json,m.ordinal,m.target_candidate,m.split,m.text_hash,r.id,r.kind AS source_kind,
                    r.role AS source_role,r.text AS original,r.text_hash AS raw_hash,r.created_epoch,
                    f.id AS fragment_id,f.group_id,f.split AS fragment_split,g.split AS group_split,
                    EXISTS(SELECT 1 FROM corpus_relations e WHERE e.parse_id=r.parse_id AND e.source_id=r.id) AS quoted
                    FROM corpus_view_messages m JOIN corpus_messages r ON r.parse_id=? AND r.ordinal=m.ordinal AND r.id=m.message_id
                    JOIN corpus_view_fragments f ON f.view_id=m.view_id AND f.id=json_extract(m.data_json,'$.fragment_id')
                    JOIN corpus_view_groups g ON g.view_id=f.view_id AND g.id=f.group_id WHERE m.view_id=? AND m.ordinal=?""",
                    (v['parse_id'],v['id'],ordinal)).fetchone()
                require(row,'missing_message')
                msg=origin(row,v)
                require(msg['source_speaker']==part_spec['source_speaker'],'speaker_changed')
                q=part_spec['public_at']
                target=None
                if q['n']==n:
                    require(type(q['start']) is int and type(q['end']) is int and 0<=q['start']<q['end']<=len(msg['text']),'public_span')
                    target={'question':msg['text'][q['start']:q['end']]}
                turns.append({'n':n,'text':msg['text'],'bubbles':[msg],'public_task':target})
            require(sum(t['public_task'] is not None for t in turns)==1,'public_count')
            collected.append({'part':part,'title':part_spec['title'],'source_speaker':part_spec['source_speaker'],'turns':turns})
    return {'version':VERSION,'study_id':'en5-ling-desktop-v1','sessions':collected,
            'source_view_id':v['id'],'source_view_sha256':v['payload_hash'],'source_proof_hash':v['source_proof_hash'],
            'source_mode':'chronological_selected_excerpts','other_speaker_messages_sent':False}


def verify_sources(root):
    root=outside_repository(root)
    saved=read_private(root/'materials.private.json')
    require(build(root)==saved,'frozen_message_changed')
    contract=read_private(root/'input-contract.private.json')
    require(contract['selection_sha256']==digest((root/'selection.private.json').read_bytes()),'selection_changed')
    require(contract['materials_sha256']==digest((root/'materials.private.json').read_bytes())
            and contract['inputs_html_sha256']==digest((root/'inputs.html').read_bytes())
            and contract['assembly_sources']==script_hashes(),'input_contract_changed')
    return saved


def render(materials,scope,binding):
    esc=lambda s:escape(str(s))
    providers=scope['providers']
    chunks=['<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
            '<title>角色 · E.N5 真人输入审阅</title><style>body{font:16px/1.65 system-ui;max-width:53rem;margin:2rem auto;padding:0 1rem;color:#25382c}article{border-top:1px solid #cad4cd;padding:.55rem 0}blockquote{white-space:pre-wrap;overflow-wrap:anywhere;background:#f1f5f2;margin:.25rem 0;padding:.55rem .85rem}small{color:#486051}details{padding:.45rem 0}summary{cursor:pointer}</style><main>',
            '<h1>角色的 E.N5 桌面体验 · 30轮输入审阅</h1><p>两段各15轮。每轮使用一条微信原始文字气泡，同一段内保留发送者和原始先后顺序。选取位置之间有删节和时间跳跃。回放以桌面实际发送时间为准，原对话另一方的发言留在本机。</p>',
            '<p>第一段和第二段分别从独立空会话开始。工作台逐条发送，等人物正文实际进入聊天视口后再发下一条。以下标题、序号、原时间及审阅说明仅供阅读。</p>',
            '<h2>拟发送到哪里</h2><ul>',
            '<li>人物回复和查前发言使用官方 DeepSeek '+esc(providers['main']['name'])+'。模型按已审 v8 合同读取角色的可见人物材料、本轮及本会话的实际前文和获准检索资料。</li>',
            '<li>每轮联网判断走 Jev 的 '+esc(providers['knowledge_router']['name'])+'（'+esc(providers['knowledge_router']['base_url'])+'）。其输入可包含本轮原话及已有公开资料的线索。</li>',
            '<li>两轮附有一项允许公开检索的原文。Jev 判定需要时，先显示角色的查前发言，再将这项公开任务交给官方 DeepSeek '+esc(providers['knowledge_native']['name'])+' 深度检索；是否发起及后续是否复用，以实际回执为准。</li></ul>',
            '<p>本次只在 E.N5 隔离实例运行，记录首次模型回执、工具用量、普通正文与查前发言的首次可见时刻。未发送的部分保留到正常续跑；已发生和结果未知的调用不会因重开而补发。</p>']
    for part in materials['sessions']:
        chunks.append('<section><h2>第'+str(part['part'])+'段 · '+esc(part['title'])+'</h2>')
        for t in part['turns']:
            m=t['bubbles'][0]
            chunks.extend(['<article><b>第'+str(t['n'])+'轮</b> <small>'+esc(m['source_time'])+'</small><blockquote>'+esc(t['text'])+'</blockquote>'])
            if t['public_task']:chunks.append('<b>允许公开检索的原文</b><blockquote>'+esc(t['public_task']['question'])+'</blockquote>')
            chunks.append('</article>')
        chunks.append('</section>')
    chunks.append('<details><summary>材料与绑定摘要</summary><p>资料视图 '+esc(materials['source_view_sha256'])+'</p><p>人物版本 '+esc(binding['character_hash'])+'</p><p>完整来源定位在同目录 materials.private.json。</p></details></main></html>')
    return ''.join(chunks)


def prepare(root):
    root=outside_repository(root)
    paths,binding=binding_at(root)
    require(binding['mode']=='author_material_replay','wrong_binding_mode')
    materials=build(root)
    checksum=save_once(root/'materials.private.json',materials)
    scope=use_scope(root,mode=binding['mode'],material_sha256=checksum)
    page=render(materials,scope,binding)
    contract={'version':'en5-desktop-inputs-v1','study_id':binding['study_id'],'baseline':binding['baseline'],
              'instance_id':binding['instance_id'],'persona_id':binding['persona_id'],'sessions':binding['sessions'],
              'materials_sha256':checksum,'inputs_html_sha256':digest(page.encode()),
              'selection_sha256':digest((root/'selection.private.json').read_bytes()),'assembly_sources':script_hashes(),
              'mode':binding['mode'],'provider_scope':scope,'parts':2,'turns_per_part':15,'public_projections':2}
    save_once(root/'input-contract.private.json',contract)
    save_text_once(root/'inputs.html',page)
    verify_sources(root)
    return contract


if __name__=='__main__':
    cli=argparse.ArgumentParser(description=__doc__)
    cli.add_argument('--root',required=True,type=Path)
    args=cli.parse_args()
    print(prepare(args.root)['materials_sha256'])
