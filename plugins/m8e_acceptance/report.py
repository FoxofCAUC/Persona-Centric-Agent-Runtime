"""Generic source documentation and privacy checks after actual E6 completion."""
from contextlib import closing
from datetime import datetime,timedelta,timezone
import hashlib
import json
import sqlite3
import subprocess

from .runtime import ROOT,EVIDENCE,read
from persona_runtime.config import ConfigStore,atomic_json
from persona_runtime.paths import RuntimePaths,DEFAULT_HOME
from persona_runtime.security import unprotect

BLOCKS={
    'docs/STATUS.md':'M8E_STATUS','docs/HANDOFF.md':'M8E_HANDOFF',
    'docs/M8.E-工作台绑定运行实施记录.md':'M8E_REPORT','README.md':'M8E_README',
    'docs/acceptance/M8.E-工作台绑定运行验收手册.md':'M8E_MANUAL'}
EXTRA=('docs/acceptance/无人值守陪伴-阶段门与验收矩阵.md','docs/施工总计划/02-总施工路线图.md')


def document_hashes():
    return {name:hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in (*BLOCKS,*EXTRA)}


def privacy_check():
    cfg=ConfigStore(RuntimePaths(DEFAULT_HOME)).load();terms={}
    for label,key in (('admin_token','admin_token'),('bridge_token','bridge_token')):terms[label]=unprotect(cfg[key])
    for label,model in [('main_model',cfg['model']),('organization_model',cfg['organization']['model'])]:
        if model.get('encrypted_key'):terms[label]=unprotect(model['encrypted_key'])
    policy=read(DEFAULT_HOME/'private/qq-testing.json')
    for field in ('daily_bot','test_bot','daily_peer','test_peer'):terms[field]=policy[field]
    with closing(sqlite3.connect((DEFAULT_HOME/'data/database/runtime.sqlite3').as_uri()+'?mode=ro',uri=True)) as db:
        terms['private_persona_name']=db.execute('SELECT name FROM personas WHERE id=?',(read(EVIDENCE/'formal-owner.json')['persona_id'],)).fetchone()[0]
    names=set()
    for args in (['git','diff','--name-only','-z','HEAD'],['git','ls-files','--others','--exclude-standard','-z']):
        names.update(n for n in subprocess.check_output(args,cwd=ROOT).decode('utf-8').split('\0') if n)
    findings=[]
    for name in sorted(names):
        path=ROOT/name
        if not path.is_file():continue
        try:text=path.read_text(encoding='utf-8-sig')
        except UnicodeError:continue
        labels=[label for label,term in terms.items() if term and term in text]
        if labels:findings.append({'file':name,'categories':labels})
    result={'clean':not findings,'scanned_files':len(names),'findings':findings,'secrets_or_identifiers_printed':False}
    atomic_json(EVIDENCE/'source-privacy-check.json',result)
    if findings:raise RuntimeError('Source publication privacy check requires review; see category-only findings')
    return result


def finalize_amended():
    """Close immediate construction only, under the user's power-off amendment."""
    amendment=read(EVIDENCE/'user-schedule-amendment.json')
    assert amendment['tonight_two_hours_cancelled'] and amendment['tonight_natural_midnight_cancelled']
    assert read(EVIDENCE/'formal-new-component-short-result.json')['passed']
    assert read(EVIDENCE/'formal-exit-result.json')['passed']
    assert read(EVIDENCE/'cleanup-result.json')['complete']
    from .finish import native_totals, business_audit
    from .runtime import core_hashes
    counts=native_totals();business=business_audit(RuntimePaths(DEFAULT_HOME))
    assert business['critical_unknown_count']==business['queue_count']==0
    assert read(EVIDENCE/'formal-owner.json')['core_hashes']==core_hashes()
    common=('2026-11-08 M8.E施工与无需等待的验收已收口；按用户23:00断电安排，'
        '今晚两小时驻留和自然午夜观察中止，只保留长时连续无人值守到11月9日白天补验。'
        '不将未观察项记为通过。工作台是唯一生命周期权威，托盘驻留继续工作，软件退出后所属组件整体停止；无系统服务、自启或独立守护。'
        '版本0.8.0.dev3，结构24、配置3、导出19、桥接0.7.3。'
        'I1/I2/I3A/I3B/L1/P1完成；用户确认正式QQ回复可见、未抢焦点，托盘“运行状态”唤回已复核。'
        '新版隔离V1/V2/V3通过后，经用户明确预览批准，正式接入NapCat4.18.42和本项目独立QQ资料目录；'
        '正式短验核对原Bot身份、OneBot连接和实际在线，随后正常注销确认并释放全部已核验所属进程。'
        '新组件隔离与正式短验均零模型调用、零QQ消息，原P1可见回复证据复用。'
        f'本阶段累计{counts["background"]}后台/{counts["main"]}主回复物理调用、{counts["native_tokens"]}原生tokens，未知用量和关键未知效果均0。'
        'v8、原聊天/整理授权、自动60/240及人工免次数额保持，正式提醒关闭。'
        '验收运行根、凭据及私人派生副本已清理并复核；正式资料、业务历史和受保护备份保留。软件已退出，未提交推送。')
    boundary=('此前有效受管范围十分钟空闲CPU平均整机2.32%通过，但其源码早于最后浏览器环境和独立资料路径补修；'
        '不能当作最终组合的两小时、内存稳定或自然午夜通过。旧掉线、安全验证、错误扫码、程序注销未确认及中断样本保留原结论。'
        'v1错账号会话由用户在手机单独下线，不能冒称程序注销成功；v2同会话正常注销另有确认。'
        '新版能正常短验不证明已解决平台长期会话失效。完整G-AUTO、高级主动陪伴及一键安装器未交付。')
    next_step=('明天白天唯一剩余验收：由用户正常打开工作台，按需确认原Bot登录，保持工作台/托盘连续运行，'
        '使用新观察目录记录当前源码、组件与原生账号、十分钟空闲CPU、预热内存基线和两小时变化、积压/每日状态及备份。'
        '观察不额外制造模型调用，不重放成功或未知位置；断网、睡眠及平台会话失效单列，软件退出后观察结束。'
        '今晚自然午夜已按用户要求取消，白天可控时钟或两小时运行均不得冒充真实午夜证据。')
    bodies={
        'M8E_STATUS':common+'\n\n'+boundary+' 详情见[M8.E实施记录](M8.E-工作台绑定运行实施记录.md)。下方为历史实施时点。',
        'M8E_HANDOFF':common+'\n\n'+next_step+'\n\n权威证据为D盘exports/acceptance/M8.E-20261108下tonight-closeout、formal-new-component-short-result、formal-exit-result及cleanup-result。'+boundary,
        'M8E_REPORT':common+'\n\n'+boundary+'\n\n'+next_step,
        'M8E_README':'M8.E施工与即时验收已收口：工作台/托盘运行时自动工作，退出主动注销本次Bot并释放所属组件；不注册系统服务或自启。按用户断电安排，长时连续无人值守留到11月9日白天补验，不宣称稳定性全部通过。详见[当前状态](docs/STATUS.md)及[实施记录](docs/M8.E-工作台绑定运行实施记录.md)。',
        'M8E_MANUAL':'状态：施工与即时验收完成，正式新版组件短验、正常注销/整体退出和清理已核验。按用户23:00断电安排，今晚两小时及自然午夜中止，仅长时连续无人值守留11月9日白天补验；未观察项不计通过。详见[实施记录](../M8.E-工作台绑定运行实施记录.md)。'}
    for name,marker in BLOCKS.items():
        text=(ROOT/name).read_text(encoding='utf-8');start='<!-- '+marker+'_BEGIN -->';end='<!-- '+marker+'_END -->'
        assert text.count(start)==text.count(end)==1
        a=text.index(start)+len(start);b=text.index(end)
        text=text[:a]+'\n'+bodies[marker]+'\n'+text[b:]
        if name=='docs/HANDOFF.md':
            text=text.replace('# 交接：M8.E正式QQ暂停，N1未完成','# 交接：M8.E施工收口，长时验收留白天',1)
            text=text.replace('当前接续为 [M8.E施工记录]', '## 以下为历史实施时点，当前接续以上方为准\n\n此前接续为 [M8.E施工记录]',1)
        if name.endswith('验收手册.md'):
            text=text.replace('| N1 | 连续运行跨自然本地午夜，积压、每日扫描、备份及资源观察 | 无额外造调用；睡眠、断网及中断分别记录 |',
                '| N1（用户调整） | 今晚两小时/自然午夜中止；11月9日白天补连续无人值守 | 仍待实测；不额外造调用，不把模拟时钟或白天当真实午夜 |')
            text=text.replace('删除未完成、正式QQ未确认或自然午夜未观察均不能标M8.E通过。',
                '清理未完成或正式QQ未确认不能收口。用户已调整今晚长时门：施工与即时验收可先结束，长时连续运行单列白天待补；不得写全部稳定性验收通过。')
        (ROOT/name).write_text(text,encoding='utf-8')
    name='docs/acceptance/无人值守陪伴-阶段门与验收矩阵.md';text=(ROOT/name).read_text(encoding='utf-8')
    replacements={'A08':'即时生命周期/托盘/注销实测通过；长时留白天，联合门未验收',
        'A09':'状态分层与受限恢复已验证；平台长时会话稳定待补',
        'A10':'I3A/I3B实际QQ通过；未知片保留未知，余片隔离且后续可交流',
        'A17':'工程检查及I2明确约定问候通过；正式默认关闭，高级主动未交付',
        'A19':'迁移/隔离恢复、工程故障检查及一次自动备份完整性通过；周期长时待补'}
    lines=text.splitlines()
    for index,line in enumerate(lines):
        for key,value in replacements.items():
            if line.startswith('| '+key+' |'):
                fields=line.split('|');fields[-2]=' '+value+' ';lines[index]='|'.join(fields)
    (ROOT/name).write_text('\n'.join(lines)+'\n',encoding='utf-8')
    name='docs/施工总计划/02-总施工路线图.md';text=(ROOT/name).read_text(encoding='utf-8')
    (ROOT/name).write_text(text.replace('| M8.E | 工作台绑定运行，施工中 |','| M8.E | 施工与即时验收收口，长时留白天补验 |'),encoding='utf-8')
    name='docs/M8.E-受限兼容补验预览.md';text=(ROOT/name).read_text(encoding='utf-8')
    text=text.replace(text.splitlines()[2], '当前结论：v2正确测试Bot的V1/V2/V3通过，零模型/零消息；V4长时由用户延期。用户另行批准正式切换预览，4.18.42与独立资料目录已接入，正式短验及在线注销/整体退出通过。长时稳定性待白天补验。下文保留原预览与实施时点。',1)
    text+='\n\n最终接续：今晚23:00断电，用户中止两小时及自然午夜；v2已在线正常注销并释放所属进程。正式采用独立资料目录，不复制旧共享QQ资料，不改变系统设置或其他项目。旧组件及数据库保护备份保留；新版短验不证明平台长期会话问题已解决。\n'
    (ROOT/name).write_text(text,encoding='utf-8')
    privacy=privacy_check()
    check=subprocess.run(['git','diff','--check'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    assert check.returncode==0, 'Source diff check needs review'
    result={'construction_complete':True,'no_wait_acceptance_passed':True,'long_run_pending':True,
        'all_acceptance_passed':False,'software_exited':True,'cleanup_verified':True,
        'native_calls':counts,'business':business,'source_hashes':core_hashes(),
        'privacy':privacy,'diff_check':'passed','git_committed':False,'git_pushed':False,
        'next_acceptance':'2026-11-09 daytime continuous unattended observation',
        'natural_midnight_observed':False,'G_AUTO_passed':False}
    for name in ('tonight-closeout.json','current-progress.json','decision-handoff.json'):
        atomic_json(EVIDENCE/name,result)
    return result


def finalize(n1,expected_hashes):
    if document_hashes()!=expected_hashes:raise RuntimeError('Documentation changed concurrently; retain user edits for review')
    assert n1['passed'] and read(EVIDENCE/'formal-exit-result.json')['passed'] and read(EVIDENCE/'cleanup-result.json')['complete']
    counts=n1['native_calls'];cpu=n1['idle_cpu']['mean_cpu_percent_whole_machine']
    date=datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d')
    common=(f'{date} M8.E规定范围通过：工作台存活时可托盘驻留，退出主动注销本次Bot并释放所属组件；不注册系统服务、启动项或独立宿主。'
        f'版本0.8.0.dev3，结构24、配置3、导出19、桥接0.7.3。隔离I1/I2/I3A/I3B/L1及正式P1通过，用户确认可见且未抢焦点；'
        f'十分钟空闲CPU平均整机{cpu:.2f}%，两小时内存和真实自然跨午夜观察通过，睡眠/观察间隔按实记录。'
        f'累计{counts["background"]}后台/{counts["main"]}主回复原生物理调用，固定传输夹具另计、模型网络调用0。'
        '正式提醒关闭、现有v8及私人授权不变；自动60/240，人工免次数额仍保留账目。软件现已退出，本阶段私人验收副本与凭据副本已清理，正式历史和生产备份保留。'
        '未提交推送，不宣称完整安装器、高级主动陪伴或G-AUTO通过。')
    bodies={
        'M8E_STATUS':common+' 详情见[M8.E实施记录](M8.E-工作台绑定运行实施记录.md)。下方保留实施时点。',
        'M8E_HANDOFF':common+'\n\n下一次由用户正常打开工作台进入运行，可能需要重新确认QQ登录。M8.E当前权威为D盘exports/acceptance/M8.E-20261108下final-result、N1-result、formal-exit-result和cleanup-result。旧源码、托盘崩溃、修复及付费位置原结果保留，不重放成功或未知效果。下一施工按后续项目规划，G-AUTO联合门保持未验收。',
        'M8E_REPORT':common+'\n\n工程主回归177项、兼容修复39项、最终退出/进程组/组件路径89项回归及24项专项分别通过；N1会话失效后追加16项定向和111项受影响检查通过，组间有重叠。平台曾使Bot会话失效，旧恢复逻辑误重启健康桥接；旧运行保留为中断证据，修复后重新登录、重新观察，不继承为连续通过。托盘原生重入、引用清理、整组优先级及退出元数据差异单独保留，成功计费位置不重跑。一次主进程退出中间快照随后已自行结束，不记成已证实的持续残留，未执行强制回收。正式受保护备份和隔离恢复通过；仅迁移/接入已确认日常角色范围。已验证中文空格路径、复用本应用组件与显式选择外部源，不再默认依赖其他项目目录；仍依赖上游组件与已安装QQ，完整一键安装部署留后续。下方为实施时点和历史限制。',
        'M8E_README':'M8.E规定范围已交付：工作台运行及托盘驻留期间自动工作，关闭软件主动注销本次Bot并释放所属组件；不注册系统服务、登录任务或开机自启。实际QQ、资源和自然跨午夜证据分别见[实施记录](docs/M8.E-工作台绑定运行实施记录.md)及[验收手册](docs/acceptance/M8.E-工作台绑定运行验收手册.md)。约定提醒默认关闭，完整安装器与G-AUTO尚未交付。',
        'M8E_MANUAL':'状态：规定范围通过；工程、实际QQ、资源、自然午夜和清理分别留证。软件已正常退出；下次由用户启动。详见[实施记录](../M8.E-工作台绑定运行实施记录.md)。'}
    updates={}
    for name,marker in BLOCKS.items():
        text=(ROOT/name).read_text(encoding='utf-8');start='<!-- '+marker+'_BEGIN -->';end='<!-- '+marker+'_END -->'
        assert text.count(start)==text.count(end)==1
        a=text.index(start)+len(start);b=text.index(end);text=text[:a]+'\n'+bodies[marker]+'\n'+text[b:]
        if name=='docs/HANDOFF.md':text=text.replace('# 交接：M8.E正式验证完成，运行观察中','# 交接：M8.E规定范围完成，软件已退出',1)
        if name=='docs/HANDOFF.md':text=text.replace('# 交接：M8.E正式QQ暂停，N1未完成','# 交接：M8.E规定范围完成，软件已退出',1)
        if name=='README.md':text=text.replace('后台仍随工作台启停，不宣称无人值守或G-AUTO通过。','后台随工作台和托盘运行，软件退出后停止；整个G-AUTO尚未通过。')
        updates[name]=text
    for name in EXTRA:
        text=(ROOT/name).read_text(encoding='utf-8')
        if name.startswith('docs/acceptance/'):
            text=text.replace('| 待M8.E实测，不以源码完成代替 |','| M8.E规定范围实测通过；G-AUTO联合门未验收 |')
        else:text=text.replace('| M8.E | 工作台绑定运行，施工中 |','| M8.E | 工作台绑定运行，规定范围完成 |')
        updates[name]=text
    for name,text in updates.items():(ROOT/name).write_text(text,encoding='utf-8')
    privacy=privacy_check()
    result=subprocess.run(['git','diff','--check'],cwd=ROOT,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    if result.returncode:raise RuntimeError('Source diff check needs review')
    return {'documents_updated':True,'privacy':privacy,'diff_check':'passed','git_write_actions':False}
