"""Preparation only: SQLite capabilities on artificial in-memory text, no runtime access."""
import argparse
import json
from pathlib import Path
import sqlite3


def probe():
    rows = [('book', '虚构书店阅读角预算三百元'), ('code', 'Python add(a, b) returns a + b')]
    queries = ['阅读角', '预算', 'Python', '不存在的事实']
    report = {'sqlite_version': sqlite3.sqlite_version, 'synthetic_only': True,
              'production_database_accessed': False, 'provider_calls': 0, 'tokenizers': {}}
    with sqlite3.connect(':memory:') as db:
        db.execute('PRAGMA temp_store=MEMORY')
        for tokenizer in ('unicode61', 'trigram'):
            try:
                db.execute(f"CREATE VIRTUAL TABLE probe USING fts5(id UNINDEXED, text, tokenize='{tokenizer}')")
                db.executemany('INSERT INTO probe VALUES (?, ?)', rows)
                report['tokenizers'][tokenizer] = {'available': True, 'matches': {
                    q: [r[0] for r in db.execute('SELECT id FROM probe WHERE probe MATCH ? ORDER BY id', ('"'+q+'"',))]
                    for q in queries}}
            except sqlite3.OperationalError as error:
                report['tokenizers'][tokenizer] = {'available': False, 'reason': str(error)}
            finally:
                db.execute('DROP TABLE IF EXISTS probe')
        report['substring'] = {q: [key for key, text in rows if q.casefold() in text.casefold()] for q in queries}
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True, help='D-drive evidence location outside the repository')
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]; output = args.output.resolve()
    if output == repo or repo in output.parents:
        parser.error('write machine evidence outside the repository')
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists(): parser.error('evidence exists: choose a new file; do not overwrite prior results')
    result = probe()
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    print(json.dumps(result, ensure_ascii=True, indent=2))
