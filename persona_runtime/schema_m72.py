"""M7.2 append-only context audit; no duplicate prompt or memory authority."""
TABLES12=('context_manifests',)
SCHEMA12="""
ALTER TABLE turns ADD COLUMN context_policy_snapshot TEXT;
CREATE TABLE context_manifests (
 id TEXT PRIMARY KEY, persona_id TEXT REFERENCES personas(id), user_id TEXT NOT NULL REFERENCES users(id),
 task_type TEXT NOT NULL CHECK(task_type IN ('dialogue','reflection','affect','director','planning')),
 turn_id TEXT REFERENCES turns(id), organization_task_id TEXT REFERENCES organization_tasks(id),
 attempt_id TEXT UNIQUE REFERENCES attempts(id), organization_attempt_id TEXT UNIQUE REFERENCES organization_attempts(id),
 manifest_json TEXT NOT NULL, created_at TEXT NOT NULL,
 CHECK((turn_id IS NOT NULL AND organization_task_id IS NULL AND organization_attempt_id IS NULL AND task_type='dialogue')
    OR (turn_id IS NULL AND organization_task_id IS NOT NULL AND attempt_id IS NULL AND task_type='reflection'))
);
CREATE INDEX context_manifests_turn ON context_manifests(turn_id,created_at);
CREATE INDEX context_manifests_task ON context_manifests(organization_task_id,created_at);
CREATE TRIGGER context_manifest_scope BEFORE INSERT ON context_manifests BEGIN
 SELECT CASE WHEN NEW.user_id!='00000000-0000-4000-8000-000000000001' THEN RAISE(ABORT,'context scope mismatch') END;
 SELECT CASE WHEN NEW.turn_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM turns t WHERE t.id=NEW.turn_id AND t.persona_id IS NEW.persona_id)
 THEN RAISE(ABORT,'context scope mismatch') END;
 SELECT CASE WHEN NEW.attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM attempts a WHERE a.id=NEW.attempt_id AND a.turn_id=NEW.turn_id)
 THEN RAISE(ABORT,'context attempt mismatch') END;
 SELECT CASE WHEN NEW.organization_task_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM organization_tasks t WHERE t.id=NEW.organization_task_id AND t.persona_id=NEW.persona_id AND t.user_id=NEW.user_id)
 THEN RAISE(ABORT,'context scope mismatch') END;
 SELECT CASE WHEN NEW.organization_attempt_id IS NOT NULL AND NOT EXISTS(SELECT 1 FROM organization_attempts a WHERE a.id=NEW.organization_attempt_id AND a.task_id=NEW.organization_task_id)
 THEN RAISE(ABORT,'context attempt mismatch') END;
END;
CREATE TRIGGER context_manifest_immutable_update BEFORE UPDATE ON context_manifests BEGIN SELECT RAISE(ABORT,'context manifest is append only'); END;
CREATE TRIGGER context_manifest_immutable_delete BEFORE DELETE ON context_manifests BEGIN SELECT RAISE(ABORT,'context manifest is append only'); END;
"""
