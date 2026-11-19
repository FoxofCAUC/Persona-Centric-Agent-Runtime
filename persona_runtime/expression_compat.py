"""迁移／兼容副本暂停表达权限，完整模型回执和业务内容保持。"""
import json


def pause(db):
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'expression_asset_policies' not in tables:return {}
    for pid, revision, raw in db.execute('SELECT persona_id,revision,data_json FROM expression_asset_policies').fetchall():
        value = json.loads(raw)
        value.update(enabled=False, learning_enabled=False, learning_sessions=[])
        db.execute('UPDATE expression_asset_policies SET revision=?,data_json=? WHERE persona_id=?',
                   (revision + 1, json.dumps(value, ensure_ascii=False), pid))
    cancelled = db.execute("UPDATE expression_learning_jobs SET state='cancelled',error_code='migration_requires_review' WHERE state='prepared'").rowcount
    unknown = db.execute("UPDATE expression_learning_jobs SET state='unknown',error_code='migration_requires_review' WHERE state='dispatched' AND raw_response IS NULL").rowcount
    result={'expression_policies_paused': True, 'expression_prepared_cancelled': cancelled,
            'expression_unreceived_preserved_unknown': unknown}
    if 'expression_runtime_policies' in tables:result.update(pause_runtime(db))
    if 'expression_speech_units' in tables:result.update(pause_speech(db))
    if 'knowledge_control' in tables:
        from .scene_policy import pause as pause_scene
        result['scene_policies_paused']=pause_scene(db)
        db.execute("""UPDATE knowledge_control SET value=json_set(value,'$.enabled',json('false'),
            '$.revision',json_extract(value,'$.revision')+1) WHERE key GLOB 'persona.import_style.*'
            AND json_valid(value) AND json_type(value,'$.enabled')='true'""")
    if 'dialogue_generation_policies' in tables:
        from .schema_dialogue import pause as pause_dialogue
        result.update(pause_dialogue(db))
    return result


def pause_speech(db, *, scene_persona=None):
    from .models import timestamp
    from .expression_utterance_publication import project_notice
    sql = """SELECT p.* FROM expression_speech_publications p JOIN expression_speech_units u
             ON u.id=p.utterance_id WHERE p.state NOT IN ('closed','complete')"""
    args = ()
    reason = 'migration_requires_review'
    if scene_persona is None:
        db.execute("UPDATE expression_speech_runs SET state='cancelled',error_code='migration_requires_review' WHERE state='prepared'")
        db.execute("UPDATE expression_speech_runs SET state='unknown',error_code='migration_requires_review' WHERE state='dispatched' AND raw_response IS NULL")
    else:
        sql += " AND u.source_kind='scene_reply' AND u.persona_id=?"
        args = (scene_persona,)
        reason = 'scene_route_disabled'
    for pub in db.execute(sql,args).fetchall():
        unit=db.execute('SELECT * FROM expression_speech_units WHERE id=?',(pub['utterance_id'],)).fetchone()
        visible=[]
        for i,row in enumerate(db.execute('SELECT ordinal,text FROM expression_speech_visible WHERE publication_id=? ORDER BY ordinal',(pub['id'],))):
            if row['ordinal']!=i:break
            visible.append(row['text'])
        if visible and pub['canonical_event_id'] is None:
            event=project_notice(db,dict(unit),''.join(visible),False)
            db.execute('UPDATE expression_speech_publications SET canonical_event_id=? WHERE id=?',(event,pub['id']))
        db.execute("UPDATE expression_speech_items SET state='cancelled' WHERE publication_id=? AND state='pending'",(pub['id'],))
        db.execute("UPDATE expression_speech_items SET state='unknown' WHERE publication_id=? AND state='offered'",(pub['id'],))
        db.execute("UPDATE expression_speech_publications SET state='closed',reason=? WHERE id=?",(reason,pub['id']))
        if unit['source_kind']=='scene_reply' and not visible and not pub['canonical_event_id']:
            db.execute("UPDATE turns SET status='interrupted',error_code=? WHERE id=? AND status IN ('queued','running')",
                       (reason,unit['turn_id']))
        db.execute('UPDATE expression_speech_units SET closed_at=coalesce(closed_at,?) WHERE id=?',(timestamp(),unit['id']))
    if scene_persona is None:
        db.execute('UPDATE expression_speech_units SET closed_at=? WHERE closed_at IS NULL',(timestamp(),))
    return {'character_outlet_paused':True,'speech_unknown_receipts_preserved':True}


def pause_runtime(db):
    from .models import identifier,timestamp,LOCAL_USER_ID
    for pid,revision,raw in db.execute('SELECT persona_id,revision,data_json FROM expression_runtime_policies').fetchall():
        value=json.loads(raw);value['enabled']=False
        db.execute('UPDATE expression_runtime_policies SET revision=?,data_json=? WHERE persona_id=?',(revision+1,json.dumps(value),pid))
    db.execute("UPDATE expression_runs SET state='cancelled',error_code='migration_requires_review' WHERE state='prepared'")
    db.execute("UPDATE expression_runs SET state='unknown',error_code='migration_requires_review' WHERE state='dispatched' AND raw_response IS NULL")
    # 只把已有回执的可见前缀投影给旧读取器，隐藏主稿和未知片段保持在证据表。
    for pub in db.execute("SELECT id,turn_id,canonical_event_id FROM expression_publications WHERE state NOT IN ('closed','complete')").fetchall():
        body=''.join(r[0] for r in db.execute('SELECT text FROM expression_utterances WHERE publication_id=? ORDER BY ordinal',(pub[0],)))
        event=pub[2]
        if body and event is None:
            turn=db.execute('SELECT session_id,persona_id FROM turns WHERE id=?',(pub[1],)).fetchone();event=identifier()
            db.execute("INSERT INTO events(id,session_id,persona_id,user_id,role,text,created_at) VALUES(?,?,?,?,'persona',?,?)",(event,turn[0],turn[1],LOCAL_USER_ID,body,timestamp()))
            db.execute("UPDATE turns SET reply_event_id=?,status='completed',cancelled_at=NULL WHERE id=?",(event,pub[1]))
        if not body:db.execute("UPDATE turns SET status='interrupted',error_code='migration_requires_review' WHERE id=?",(pub[1],))
        db.execute("UPDATE expression_presentation_items SET state='cancelled' WHERE publication_id=? AND state='pending'",(pub[0],))
        db.execute("UPDATE expression_presentation_items SET state='unknown' WHERE publication_id=? AND state='offered'",(pub[0],))
        db.execute("UPDATE expression_publications SET state='closed',reason='migration_requires_review',canonical_event_id=? WHERE id=?",(event,pub[0]))
    db.execute('UPDATE expression_runtime_turns SET closed_at=? WHERE closed_at IS NULL',(timestamp(),))
    return {'expression_runtime_paused':True,'unseen_expression_not_promoted':True}
