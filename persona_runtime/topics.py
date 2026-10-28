"""Human annotations over immutable session sources; never a recall authority."""
import json
from .archive import scope, encoded, read_exact, binding
from .models import LOCAL_USER_ID, RuntimeErrorBase, identifier, timestamp
from .sessions import owned, operation, record_operation, check_revision


def annotation(title, description):
    if not isinstance(title,str) or not title.strip() or len(title)>120 or not isinstance(description,str) or len(description)>1000:
        raise RuntimeErrorBase('invalid_topic_annotation')


class TopicService:
    def __init__(self,store):self.store=store

    @staticmethod
    def _table(kind):
        if kind not in ('topic','thread'):raise RuntimeErrorBase('invalid_topic_kind')
        return 'topic_segments' if kind=='topic' else 'conversation_threads'

    def _row(self,conn,kind,object_id,selected):
        r=conn.execute(f'SELECT * FROM {self._table(kind)} WHERE id=? AND persona_id=? AND user_id=?',
                       (object_id,selected['persona_id'],LOCAL_USER_ID)).fetchone()
        if not r or json.loads(r['scope_json'])!=selected:raise RuntimeErrorBase('topic_unavailable')
        return dict(r)

    @staticmethod
    def _version(conn,kind,row,operation_id):
        data=dict(row)
        if kind=='thread':data['segment_ids']=[r[0] for r in conn.execute('SELECT segment_id FROM thread_segments WHERE thread_id=? ORDER BY created_at,segment_id',(row['id'],))]
        conn.execute('INSERT INTO topic_versions VALUES(?,?,?,?,?,?,?,?,?,?)',
                     (identifier(),row['persona_id'],LOCAL_USER_ID,kind,row['id'],row['revision'],encoded(data),'admin',operation_id,timestamp()))

    @staticmethod
    def _members(conn,session_id,turn_ids,selected):
        owned(conn,session_id,selected)
        p=conn.execute('SELECT id,manifest_json FROM session_packages WHERE session_id=?',(session_id,)).fetchone()
        if not p:raise RuntimeErrorBase('session_not_sealed')
        if not isinstance(turn_ids,list) or not turn_ids or len(turn_ids)>1000 or any(not isinstance(t,str) for t in turn_ids) or len(set(turn_ids))!=len(turn_ids):raise RuntimeErrorBase('invalid_topic_members')
        rows=json.loads(p['manifest_json'])['turns'];ids=[t['turn_id'] for t in rows]
        try:indexes=[ids.index(t) for t in turn_ids]
        except ValueError:raise RuntimeErrorBase('invalid_topic_members') from None
        if indexes!=list(range(indexes[0],indexes[0]+len(indexes))):raise RuntimeErrorBase('topic_requires_contiguous_turns')
        for index in indexes:
            current=read_exact(conn,selected,{'kind':'turn','id':ids[index]})
            if current['status']!='found' or binding(current)!=rows[index]['binding']:raise RuntimeErrorBase('topic_source_unavailable')
        return p['id']

    def list(self,persona_id,channel='desktop',bot='',peer='',kind='topic'):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            return [self._view(conn,kind,dict(r),selected) for r in conn.execute(f'SELECT * FROM {self._table(kind)} WHERE scope_json=? ORDER BY rowid DESC',(encoded(selected),))]

    def _view(self,conn,kind,row,selected):
        row.pop('scope_json');row['source_type']='manual_annotation';row['affects_recall']=False
        if kind=='topic':
            ids=json.loads(row.pop('turn_ids_json'));row['turn_ids']=ids
            package=conn.execute('SELECT manifest_json FROM session_packages WHERE id=?',(row['package_id'],)).fetchone()
            expected={t['turn_id']:t['binding'] for t in json.loads(package[0])['turns']}
            row['unavailable_turn_ids']=[t for t in ids if binding(read_exact(conn,selected,{'kind':'turn','id':t}))!=expected.get(t)]
            row['usable']=not row['unavailable_turn_ids'] and row['state']=='active'
        else:row['segment_ids']=[r[0] for r in conn.execute('SELECT segment_id FROM thread_segments WHERE thread_id=? ORDER BY created_at,segment_id',(row['id'],))]
        return row

    def show(self,object_id,persona_id,channel='desktop',bot='',peer='',kind='topic'):
        selected=scope(persona_id,channel,bot,peer)
        with self.store.connection() as conn:
            r=self._row(conn,kind,object_id,selected);result=self._view(conn,kind,r,selected)
            result['history']=[dict(x) for x in conn.execute('SELECT revision,actor,operation_id,created_at,data_json FROM topic_versions WHERE object_id=? AND object_kind=? ORDER BY revision',(object_id,kind))]
            return result

    def create(self,persona_id,title,description,operation_id,expected_revision=0,channel='desktop',bot='',peer='',kind='topic',session_id=None,turn_ids=None):
        selected=scope(persona_id,channel,bot,peer);annotation(title,description)
        value={'title':title,'description':description,'session_id':session_id,'turn_ids':turn_ids,'revision':expected_revision}
        with self.store.connection(write=True) as conn:
            digest,old=operation(conn,operation_id,kind+'.create',selected,value)
            if old is not None:return old
            if expected_revision!=0:raise RuntimeErrorBase('session_version_conflict')
            ident=identifier();now=timestamp()
            if kind=='topic':
                package_id=self._members(conn,session_id,turn_ids,selected)
                conn.execute('INSERT INTO topic_segments VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(ident,persona_id,LOCAL_USER_ID,session_id,package_id,encoded(selected),title,description,encoded(turn_ids),1,'active',now))
            elif kind=='thread':
                if session_id is not None or turn_ids is not None:raise RuntimeErrorBase('invalid_thread_fields')
                conn.execute('INSERT INTO conversation_threads VALUES(?,?,?,?,?,?,?,?,?)',(ident,persona_id,LOCAL_USER_ID,encoded(selected),title,description,1,'active',now))
            else:raise RuntimeErrorBase('invalid_topic_kind')
            r=self._row(conn,kind,ident,selected);self._version(conn,kind,r,operation_id)
            result=self._view(conn,kind,r,selected);record_operation(conn,operation_id,kind+'.create',selected,digest,result);return result

    def update(self,object_id,persona_id,title,description,state,expected_revision,operation_id,channel='desktop',bot='',peer='',kind='topic'):
        selected=scope(persona_id,channel,bot,peer);annotation(title,description)
        if state not in ('active','archived'):raise RuntimeErrorBase('invalid_topic_state')
        with self.store.connection(write=True) as conn:
            value={'id':object_id,'title':title,'description':description,'state':state,'revision':expected_revision}
            digest,old=operation(conn,operation_id,kind+'.update',selected,value)
            if old is not None:return old
            r=self._row(conn,kind,object_id,selected);check_revision(r,expected_revision)
            conn.execute(f'UPDATE {self._table(kind)} SET title=?,description=?,state=?,revision=revision+1 WHERE id=?',(title,description,state,object_id))
            r=self._row(conn,kind,object_id,selected);self._version(conn,kind,r,operation_id)
            result=self._view(conn,kind,r,selected);record_operation(conn,operation_id,kind+'.update',selected,digest,result);return result

    def link(self,thread_id,segment_id,persona_id,expected_revision,segment_revision,operation_id,linked=True,channel='desktop',bot='',peer=''):
        selected=scope(persona_id,channel,bot,peer)
        if type(linked) is not bool:raise RuntimeErrorBase('invalid_link')
        with self.store.connection(write=True) as conn:
            value={'thread_id':thread_id,'segment_id':segment_id,'revision':expected_revision,'segment_revision':segment_revision,'linked':linked}
            digest,old=operation(conn,operation_id,'thread.link',selected,value)
            if old is not None:return old
            thread=self._row(conn,'thread',thread_id,selected);segment=self._row(conn,'topic',segment_id,selected)
            check_revision(thread,expected_revision);check_revision(segment,segment_revision)
            if linked:
                if thread['state']!='active' or segment['state']!='active':raise RuntimeErrorBase('topic_archived')
                self._members(conn,segment['session_id'],json.loads(segment['turn_ids_json']),selected)
                prior=conn.execute('SELECT thread_id FROM thread_segments WHERE segment_id=?',(segment_id,)).fetchone()
                if prior and prior[0]!=thread_id:raise RuntimeErrorBase('unlink_previous_thread_first')
                conn.execute('INSERT OR IGNORE INTO thread_segments VALUES(?,?,?,?,?)',(thread_id,segment_id,persona_id,LOCAL_USER_ID,timestamp()))
            else:conn.execute('DELETE FROM thread_segments WHERE thread_id=? AND segment_id=?',(thread_id,segment_id))
            for kind,ident in (('thread',thread_id),('topic',segment_id)):
                conn.execute(f'UPDATE {self._table(kind)} SET revision=revision+1 WHERE id=?',(ident,))
                self._version(conn,kind,self._row(conn,kind,ident,selected),operation_id)
            result={'thread_revision':expected_revision+1,'segment_revision':segment_revision+1,'linked':linked}
            record_operation(conn,operation_id,'thread.link',selected,digest,result);return result
