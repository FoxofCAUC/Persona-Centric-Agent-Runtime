"""结构33。兼容副本保留两个模块的完整表集，再升级时原地校验。"""
from . import schema_dialogue_generation, schema_dialogue_sources
from .models import RuntimeErrorBase

TABLES = (*schema_dialogue_sources.TABLES, *schema_dialogue_generation.TABLES)


def check(present):
    if not set(TABLES).issubset(present):
        raise RuntimeErrorBase('dialogue_schema_incomplete')


def migrate(db):
    present = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if present.intersection(TABLES):
        check(present)
    schema_dialogue_sources.migrate(db)
    schema_dialogue_generation.migrate(db)


def pause(db):
    result = schema_dialogue_generation.pause(db)
    result.update(schema_dialogue_sources.pause(db))
    return result
