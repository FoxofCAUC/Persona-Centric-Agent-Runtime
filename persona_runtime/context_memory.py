"""Shared canonical memory ranking, without creating or reinforcing memories."""
from .models import LOCAL_USER_ID

def rank_memories(conn,turn,text,recent_queries,*,enforce_scope=False):
    from .chat import terms
    current_terms = terms(text)
    previous_terms = terms(' '.join(recent_queries))
    ranked = []
    sql="SELECT * FROM memories WHERE persona_id=? AND user_id=? AND status='active'"
    args=[turn['persona_id'],LOCAL_USER_ID]
    for memory in conn.execute(sql,args):
        memory = dict(memory)
        from .automatic_memory import usable_fact
        if not usable_fact(conn,memory):continue
        if enforce_scope:
            from .context_builders import memory_usable
            if not memory_usable(conn,turn,memory):continue
        mt = terms(memory['text'])
        current, history = sorted(mt & current_terms), sorted(mt & previous_terms)
        score = 3*len(current)+len(history)
        if text.strip() and text.strip().lower() in memory['text'].lower():
            score += 9
        if not score and memory['previous_id']:
            # A current revision must accompany an old source present in raw
            # context even if the user's new query contains no matching term.
            old=conn.execute('SELECT source_event_id FROM memories WHERE id=?',(memory['previous_id'],)).fetchone()
            if old:
                original=conn.execute('SELECT session_id FROM events WHERE id=?',(old[0],)).fetchone()
                if original and original[0]==turn['session_id']:score=1
        if score:
            ranked.append((score, memory['updated_at'], memory['id'], memory, current, history))
    ranked.sort(key=lambda r: r[:3], reverse=True)
    return ranked
