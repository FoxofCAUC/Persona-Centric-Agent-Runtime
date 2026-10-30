"""v18: widen only the new reply contract; preserve all historical rows."""
import re


def migrate(conn):
    triggers=list(conn.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger'"))
    for name,_ in triggers:conn.execute('DROP TRIGGER "'+name+'"')
    for table in ('reply_batches','reply_segments'):
        sql=conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?",(table,)).fetchone()[0]
        indexes=[r[0] for r in conn.execute("SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",(table,))]
        if table=='reply_batches':
            sql=sql.replace('schema_version IN (1,2)','schema_version IN (1,2,3)')
            sql=sql.replace('CHECK(segment_count BETWEEN 1 AND 3)',
                'CHECK(segment_count BETWEEN 1 AND CASE WHEN schema_version=3 THEN 12 ELSE 3 END)')
        else:
            sql=sql.replace('CHECK(segment_index BETWEEN 1 AND 3)','CHECK(segment_index BETWEEN 1 AND 12)')
        sql=re.sub(r'CREATE TABLE\s+"?'+table+r'"?', 'CREATE TABLE bubble_new_'+table,sql,count=1,flags=re.I)
        conn.execute(sql)
        conn.execute(f'INSERT INTO bubble_new_{table} SELECT * FROM {table}')
        conn.execute(f'DROP TABLE {table}')
        conn.execute(f'ALTER TABLE bubble_new_{table} RENAME TO {table}')
        for index in indexes:conn.execute(index)
    for _,sql in triggers:conn.execute(sql)
    for action in ('INSERT','UPDATE'):
        conn.execute(f'''CREATE TRIGGER bubble_segment_{action.lower()} BEFORE {action} ON reply_segments
        BEGIN SELECT CASE WHEN NOT EXISTS (
            SELECT 1 FROM reply_batches b WHERE b.id=NEW.batch_id
            AND NEW.segment_index<=b.segment_count
            AND NEW.segment_index<=CASE WHEN b.schema_version=3 THEN 12 ELSE 3 END
        ) THEN RAISE(ABORT,'reply segment contract mismatch') END; END''')
    conn.execute('''CREATE TRIGGER bubble_batch_update BEFORE UPDATE ON reply_batches
        BEGIN SELECT CASE WHEN EXISTS (SELECT 1 FROM reply_segments s WHERE s.batch_id=NEW.id
        AND s.segment_index>NEW.segment_count) THEN RAISE(ABORT,'reply batch contract mismatch') END; END''')
