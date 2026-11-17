"""静态人物素材与提示装配；不读取业务库、凭据或派发模型。"""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import subprocess

from persona_runtime.models import PersonaSpec,generic_persona

ROOT=Path(__file__).resolve().parent
REPO=ROOT.parents[1]
VERSION='persona-reply-n0-bundle-v1'
MAX_REQUEST_CHARACTERS=60000
UNDERSTANDING_MODEL={'base_url':'https://api.deepseek.com','name':'deepseek-flash','thinking':'disabled','temperature':0.3,'max_tokens':1024,'response_format':'text','tools':False}
REPLYER_MODEL={'base_url':'https://api.deepseek.com','name':'deepseek-v4-pro','thinking':'disabled','temperature':1.0,'max_tokens':8192,'response_format':'text','tools':False}


def encoded(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))
def sha(data):return hashlib.sha256(data).hexdigest()
def digest(value):return sha(encoded(value).encode())
def read(path):return json.loads(path.read_text(encoding='utf-8-sig'))


def require_active_candidate():
    """作者已否决此候选；旧生成和审阅入口不能恢复它。"""
    raise ValueError('n0_candidate_rejected_by_author')


def rejection_result():
    return {'status':'rejected_by_author','author_material_gate_passed':False,
            'reason':'developer_authored_language_rejected','dispatch_performed':False,'real_model_requests':0}


def prompt(name):return (ROOT/'prompts'/(name+'.prompt')).read_text(encoding='utf-8').strip()


def history_text(scene):
    require_active_candidate()
    lines=[]
    for item in scene.get('history',[]):
        if item['speaker'] not in ('user','persona'):raise ValueError('n0_history_requires_conversation_speaker')
        lines.append(('对方' if item['speaker']=='user' else '你')+'：'+item['text'])
    return '\n'.join(lines)


def context_text(scene):
    require_active_candidate()
    sections=[]
    if scene.get('history'):sections.append('你们刚才聊到：\n'+history_text(scene))
    if scene.get('relationship'):sections.append('你们的关系：\n'+scene['relationship'])
    if scene.get('memories'):sections.append('与这次有关的已有记忆：\n'+'\n'.join(scene['memories']))
    sections.append('对方现在说：\n'+scene['user'])
    if scene.get('reference'):sections.append('这次用得上的资料：\n'+scene['reference'])
    return sections


def checked_messages(messages):
    if len(messages)!=2 or [m['role'] for m in messages]!=['system','user']:raise ValueError('n0_message_structure')
    if len(encoded(messages))>MAX_REQUEST_CHARACTERS:raise ValueError('n0_request_capacity')
    return messages


def understanding_messages(role,scene):
    require_active_candidate()
    profile=role['proposed_profile']
    identity='聊天中的人物：'+profile['name']+'。'+profile['introduction']
    return checked_messages([{'role':'system','content':prompt('understanding')},
                             {'role':'user','content':'\n\n'.join([identity,*context_text(scene)])}])


def replyer_messages(role,scene,*,notes=None,assets=(),phase='main'):
    require_active_candidate()
    if phase not in ('main','search_notice'):raise ValueError('n0_phase_invalid')
    if len(assets)>5:raise ValueError('n0_asset_capacity')
    if any(a.get('role_id')!=role['id'] for a in assets):raise ValueError('n0_cross_role_asset')
    if phase=='search_notice' and notes is not None:raise ValueError('n0_notice_has_no_understanding_call')
    profile=role['proposed_profile']
    voice=profile['introduction']+'性格'+ '、'.join(profile['core_traits'])+'。'+profile['initial_background']+'\n'+profile['expression_style']
    fixed=prompt('replyer' if phase=='main' else 'search_notice').format(identity=profile['name'],voice=voice)
    sections=context_text(scene)
    if notes:sections.append('对话情况：\n'+notes)
    if assets:
        sections.append('可以参考的说话习惯：\n'+'\n'.join('当'+a['situation']+'时，'+a['style'] for a in assets))
    return checked_messages([{'role':'system','content':fixed},{'role':'user','content':'\n\n'.join(sections)}])


def make_preview(role,scene,assets,*,ident=None,phase='main'):
    require_active_candidate()
    notes=scene.get('static_understanding') if phase=='main' else None
    return {'id':ident or role['id']+'-'+scene['id'],'role_id':role['id'],'phase':phase,'user':scene['user'],
        'history':deepcopy(scene.get('history',[])),
        'context_source':'developer_authored_synthetic_not_formal_conversation',
        'understanding_note_source':'development_agent_static_illustration_not_model_output' if phase=='main' else None,
        'selected_asset_ids':[a['id'] for a in assets],'selection_method':'static_preview_not_runtime_retrieval',
        'understanding':{'model':dict(UNDERSTANDING_MODEL),'messages':understanding_messages(role,scene),'illustrated_note':notes} if phase=='main' else None,
        'replyer':{'model':dict(REPLYER_MODEL),'messages':replyer_messages(role,scene,notes=notes,assets=assets,phase=phase)},
        'real_model_requests':0,'user_review_samples_in_model_input':False}


def supplemental_previews(roles):
    require_active_candidate()
    by_id={r['id']:r for r in roles}
    cases=[
        ('notice-xiazhi','xiazhi','search_notice',{
            'user':'帮我查一下那个摄影展这周末的开放时间，查到再告诉我。','history':[],
            'reference':'准备查开放时间，搜索尚未开始，目前没有新的搜索结果。'},[]),
        ('reference-found-qinghe','qinghe','main',{
            'user':'就按刚才那份展览说明，帮我算一下周六几点前能进去。',
            'history':[{'speaker':'persona','text':'我去看一下展方这周的说明。'}],
            'reference':'展览说明：周六10:00至18:00开放，17:30停止入场。',
            'static_understanding':'当前资料给出的周六开放时间是10:00至18:00，停止入场时间是17:30。用户问的重点是最晚入场时间。'},['QH-09']),
        ('reference-unavailable-ningzhou','ningzhou','main',{
            'user':'那现在就是还不能确认新版支持离线导出，对吧？先别继续查了。',
            'history':[{'speaker':'persona','text':'我去找一下新版的说明。'}],
            'reference':'本次没有完整取得新版的功能说明，是否支持离线导出尚未确认。',
            'static_understanding':'新版是否支持离线导出尚未确认，对方要求停止新增查找。'},['NZ-08']),
        ('memory-correction-linchuan','linchuan','main',{
            'user':'纠正一下，不是我不吃辣，只是不喜欢太麻，微辣其实可以。',
            'history':[{'speaker':'persona','text':'那你不吃辣，点清淡的？'}],
            'relationship':'你们已经聊过点菜偏好。',
            'memories':['先前对方提过不太喜欢重麻的口味。'],
            'static_understanding':'对方把偏好说清：微辣可以，不喜欢的是过于麻的口味；先前把它理解成不吃辣有偏差。'},['LC-05'])
    ]
    previews=[]
    for ident,rid,phase,scene,ids in cases:
        role=by_id[rid];assets=[a for a in role['assets'] if a['id'] in ids]
        previews.append(make_preview(role,scene,assets,ident=ident,phase=phase))
    return previews


def build():
    require_active_candidate()
    catalog=read(ROOT/'materials.json')
    original_path=REPO/catalog['role_source'];original=read(original_path)['personas']
    roles=[]
    for candidate in catalog['roles']:
        rid=candidate['id'];profile=generic_persona().to_dict()
        profile.update(original[rid],initial_background='和对方从这次聊天开始认识。')
        PersonaSpec.from_dict(profile)
        proposed=profile|{'expression_style':candidate['voice']}
        PersonaSpec.from_dict(proposed)
        roles.append(candidate|{'original_profile':profile,'proposed_profile':proposed,
            'assets':[a|{'role_id':rid,'source_kind':'authored_examples','authoring':'development_agent_static_composition',
                        'permission_after_author_approval':['use'],'automatic_learning_eligible':False,'state':'draft_not_registered'} for a in candidate['assets']]})
    previews=[]
    for role in roles:
        for scene in catalog['pilot_scenes']:
            selected=[a for a in role['assets'] if a['id'].split('-')[-1] in scene['preview_asset_suffixes']]
            previews.append(make_preview(role,scene,selected))
    prompts={name:prompt(name) for name in ('understanding','replyer','search_notice')}
    paths=[ROOT/'materials.json',*sorted(ROOT.glob('*.py')),*[ROOT/'prompts'/(n+'.prompt') for n in prompts],original_path,REPO/'persona_runtime/models.py',
           REPO/'experiments/expression_ab_conversation/study.py',REPO/'docs/expression/E阶段重构-人物回复链路开发指导.md']
    sources={p.relative_to(REPO).as_posix():sha(p.read_bytes()) for p in paths}
    bundle={'version':VERSION,'date':'2026-11-17','status':'pending_author_review','authoring':catalog['authorship'],
        'head':subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO).decode().strip(),
        'source_hashes':sources,'roles':roles,'review_scenes':catalog['review_scenes'],'pilot_scenes':catalog['pilot_scenes'],
        'prompts':prompts,'pilot_previews':previews,'supplemental_previews':supplemental_previews(roles),
        'next_trial':{'stage':'E.N1','positions':16,'chains':['v8_flash','v8_pro','flash_understanding_pro_replyer'],
            'maximum_requests':64,'allocated_to_v8_flash':16,'allocated_to_v8_pro':16,'allocated_to_understanding':16,
            'allocated_to_replyer':16,'native_requests':0,'jev_requests':0,'dispatch_authorized':False},
        'real_model_requests':0,'formal_config_modified':False,'assets_registered':False,'runtime_or_schema_changed':False}
    validate(bundle)
    return bundle


def validate(bundle):
    expected=['qinghe','xiazhi','ningzhou','linchuan']
    if bundle.get('version')!=VERSION or [r['id'] for r in bundle['roles']]!=expected:raise ValueError('n0_roles_invalid')
    identifiers=[]
    for role in bundle['roles']:
        if len(role['assets'])!=12 or len(role['review_samples'])!=4:raise ValueError('n0_material_counts')
        if {s['scene'] for s in role['review_samples']}!={s['id'] for s in bundle['review_scenes']}:raise ValueError('n0_review_scene_binding')
        for key,value in role['original_profile'].items():
            if key!='expression_style' and role['proposed_profile'][key]!=value:raise ValueError('n0_personality_authority_changed')
        for asset in role['assets']:
            identifiers.append(asset['id'])
            if asset['role_id']!=role['id'] or asset['automatic_learning_eligible'] is not False:raise ValueError('n0_asset_scope')
    if len(identifiers)!=len(set(identifiers)) or len(identifiers)!=48:raise ValueError('n0_duplicate_assets')
    if len(bundle['pilot_previews'])!=16 or len({p['id'] for p in bundle['pilot_previews']})!=16:raise ValueError('n0_pilot_positions')
    if bundle['next_trial']['maximum_requests']!=64 or bundle['next_trial']['dispatch_authorized'] is not False:raise ValueError('n0_next_trial_not_authorized')
    if bundle['real_model_requests']!=0:raise ValueError('n0_no_physical_calls')
    return {'roles':4,'assets':48,'review_samples':16,'pilot_positions':16,'supplemental_previews':4,'model_requests':0}


def review_sections(bundle):
    result=[]
    for role in bundle['roles']:
        content={k:role[k] for k in ('id','original_profile','proposed_profile','assets','review_samples')}
        result.append({'id':'role:'+role['id'],'kind':'role','sha256':digest(content)})
    for name,value in bundle['prompts'].items():result.append({'id':'prompt:'+name,'kind':'prompt','sha256':digest(value)})
    return result


def verify_review(bundle,value):
    """保留调用签名用于旧回执读取；任何旧意见均不能放行已否决候选。"""
    return rejection_result()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=('render','verify-review'))
    parser.add_argument('--output',type=Path,required=True);parser.add_argument('--review',type=Path)
    args=parser.parse_args();out=args.output.resolve()
    if out.is_relative_to(REPO):raise ValueError('n0_output_outside_source_required')
    if args.action=='render':
        from .review import render
        bundle=build();print(json.dumps(render(bundle,out),ensure_ascii=False));return
    if not args.review:parser.error('review file required')
    bundle=read(out/'bundle.json')
    if digest(bundle)!=read(out/'bundle-hash.json')['sha256']:raise ValueError('n0_saved_bundle_changed')
    validate(bundle);print(json.dumps(verify_review(bundle,read(args.review)),ensure_ascii=False))


if __name__=='__main__':main()
