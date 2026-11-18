"""Schema 33. Scope a reviewed assembly and its immutable external supports."""

from .storage import execute_schema


TABLES = ('dialogue_source_bundles', 'dialogue_corpus_supports')


def migrate(db):
    execute_schema(db, '''
        CREATE TABLE IF NOT EXISTS dialogue_source_bundles (
            id TEXT PRIMARY KEY, persona_id TEXT NOT NULL REFERENCES personas(id),
            scope_key TEXT NOT NULL, authority TEXT NOT NULL, home TEXT NOT NULL,
            purpose TEXT NOT NULL CHECK(purpose IN ('offline_replay','candidate_runtime')),
            manifest_path TEXT NOT NULL, manifest_hash TEXT NOT NULL,
            approval_path TEXT, approval_hash TEXT,
            state TEXT NOT NULL CHECK(state IN ('active','paused','revoked')),
            created_at TEXT NOT NULL,
            CHECK((purpose='offline_replay' AND approval_path IS NULL AND approval_hash IS NULL)
               OR (purpose='candidate_runtime' AND approval_path IS NOT NULL AND approval_hash IS NOT NULL))
        );
        CREATE TABLE IF NOT EXISTS dialogue_corpus_supports (
            bundle_id TEXT NOT NULL REFERENCES dialogue_source_bundles(id),
            proposal_id TEXT NOT NULL, asset_id TEXT NOT NULL, asset_revision INTEGER NOT NULL,
            ref_json TEXT NOT NULL CHECK(json_valid(ref_json)), ref_hash TEXT NOT NULL,
            PRIMARY KEY(bundle_id,proposal_id), UNIQUE(bundle_id,asset_id),
            FOREIGN KEY(asset_id,asset_revision) REFERENCES expression_asset_versions(asset_id,revision)
        );
        CREATE TRIGGER IF NOT EXISTS dialogue_source_bundle_contract BEFORE UPDATE ON dialogue_source_bundles
        WHEN NEW.id IS NOT OLD.id OR NEW.persona_id IS NOT OLD.persona_id OR NEW.scope_key IS NOT OLD.scope_key
          OR NEW.authority IS NOT OLD.authority OR NEW.home IS NOT OLD.home OR NEW.purpose IS NOT OLD.purpose
          OR NEW.manifest_path IS NOT OLD.manifest_path OR NEW.manifest_hash IS NOT OLD.manifest_hash
          OR NEW.approval_path IS NOT OLD.approval_path OR NEW.approval_hash IS NOT OLD.approval_hash
          OR NEW.created_at IS NOT OLD.created_at OR (OLD.state!='active' AND NEW.state='active')
        BEGIN SELECT RAISE(ABORT,'dialogue source contract is immutable'); END;
    ''')
    for table, actions in ((TABLES[0], ('DELETE',)), (TABLES[1], ('UPDATE','DELETE'))):
        for action in actions:
            execute_schema(db, f'''CREATE TRIGGER IF NOT EXISTS {table}_immutable_{action.lower()}
                BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT,'dialogue source evidence is immutable'); END;''')


def pause(db):
    result = db.execute("UPDATE dialogue_source_bundles SET state='paused' WHERE state='active'")
    return {'dialogue_source_bundles': result.rowcount}
