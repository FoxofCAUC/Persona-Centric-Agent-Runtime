"""Authority records for local encyclopedia packages and explicit reader grants."""

TABLES = ('knowledge_control',)
SCHEMA = """
CREATE TABLE knowledge_control (
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL CHECK(json_valid(value))
);
"""


def migrate(conn):
    from .storage import execute_schema
    execute_schema(conn, SCHEMA)
