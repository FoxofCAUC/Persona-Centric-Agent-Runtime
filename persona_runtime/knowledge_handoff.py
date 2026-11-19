"""Bind the completed public lookup to its originating turn and first main receipt."""
from .archive import fingerprint
from .models import RuntimeErrorBase,timestamp
from .prompt_assembly import PromptBlock

VERSION = 'desktop-post-search-v1'


def completed_blocks(messages, details, result, turn):
    retrieval=result.get('retrieval',{})
    note=result.get('organized') or {}
    assembly=details.get('prompt_assembly') or {}
    blocks=assembly.get('blocks',[])
    if not (turn.get('channel')=='desktop' and not turn.get('_search_notice')
            and turn.get('id') and result.get('record_id')==turn['id']
            and retrieval.get('executor')=='deepseek-native-web-v1'
            and retrieval.get('search_outcome')=='completed' and retrieval.get('material_acquired')
            and note.get('notes') and note.get('task_id') in retrieval.get('task_ids',())
            and result.get('sources') and any(b['name']=='search_notice' and b['present'] for b in blocks)):
        return None
    if assembly.get('input_snapshot_hash')!=fingerprint(messages) or messages[0]['role']!='system':
        raise RuntimeErrorBase('knowledge_input_assembly_changed')
    system=messages[0]['content'];remaining=[];offset=0
    for b in blocks:
        if b['role']!='system':continue
        content=system[offset:offset+b['characters']]
        if b['message_index']!=0 or b['start_character']!=offset or fingerprint(content)!=b['content_hash']:
            raise RuntimeErrorBase('knowledge_input_assembly_changed')
        offset+=len(content)
        if b['name']!='search_notice':
            remaining.append(PromptBlock(b['name'],content,b['owner'],b['version']))
    if offset!=len(system):raise RuntimeErrorBase('knowledge_input_assembly_changed')
    return remaining


def repeated_notice(db, turn, body, details):
    if not body or ((details.get('knowledge') or {}).get('translation') or {}).get('handoff_version')!=VERSION:
        return False
    from .knowledge_search_notice import read
    notice=read(db,turn)
    return bool(notice and ' '.join(notice['text'].split())==' '.join(body.split()))


def reject_echo(db, turn, attempt, usage):
    import json
    from .progress import record
    changed=db.execute("""UPDATE turns SET status='failed',error_code='knowledge_repeated_notice',usage_json=?,updated_at=?
        WHERE id=? AND status='running' AND execution_revision=? AND cancelled_at IS NULL""",
        (json.dumps(usage),timestamp(),turn['id'],turn['execution_revision'])).rowcount
    if changed:record(db,turn['id'],'failed',attempt)
