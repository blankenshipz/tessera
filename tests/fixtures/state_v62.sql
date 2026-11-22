BEGIN TRANSACTION;
CREATE TABLE admitted_experiment_artifact_evidence_references (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id), attempt_id TEXT NOT NULL
        REFERENCES research_experiment_admitted_attempts(id), measurement_id TEXT,
        reference_kind TEXT NOT NULL CHECK (reference_kind IN ('attempt_artifact','measurement_artifact')),
        created_at TEXT NOT NULL,
        CHECK ((reference_kind='attempt_artifact' AND measurement_id IS NULL) OR
               (reference_kind='measurement_artifact' AND measurement_id IS NOT NULL)));
CREATE TABLE agent_authority_consumptions (
        id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES agent_authority_grants(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id), action TEXT NOT NULL,
        execution_grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        attempt_id TEXT, operation_id TEXT, budget_cents INTEGER NOT NULL CHECK(budget_cents>=0),
        command_digest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agent_authority_grants (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        subject_credential_id TEXT NOT NULL REFERENCES credentials(id),
        issuer_id TEXT NOT NULL REFERENCES principals(id),
        parent_id TEXT REFERENCES agent_authority_grants(id),
        request_id TEXT REFERENCES agent_authority_requests(id),
        bounds_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL CHECK(revision>=1), epoch INTEGER NOT NULL CHECK(epoch>=1),
        reserved_cents INTEGER NOT NULL CHECK(reserved_cents>=0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE agent_authority_requests (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        actor_id TEXT NOT NULL REFERENCES principals(id),
        bounds_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agent_output_provider_bindings (
        output_manifest_id TEXT PRIMARY KEY REFERENCES artifact_manifests(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
        provider_id TEXT NOT NULL, job_uid TEXT NOT NULL,
        identity_digest TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        credential_id TEXT NOT NULL, recorded_at TEXT NOT NULL);
CREATE TABLE agent_provenance (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        credential_id TEXT NOT NULL,
        record_id TEXT NOT NULL,
        request_digest TEXT NOT NULL,
        recorded_at TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        UNIQUE(org_id, project_id, record_id));
CREATE TABLE agent_research_promotions (
        proposal_id TEXT PRIMARY KEY REFERENCES agent_research_records(id),
        result_json TEXT NOT NULL, actor_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL);
CREATE TABLE agent_research_records (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT NOT NULL UNIQUE,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        actor_id TEXT NOT NULL REFERENCES principals(id), kind TEXT NOT NULL,
        payload_json TEXT NOT NULL, digest TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE agent_sandbox_bindings (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        package_digest TEXT NOT NULL, blueprint_json TEXT NOT NULL,
        state_json TEXT NOT NULL, updated_at TEXT NOT NULL
    );
CREATE TABLE artifact_custody_releases (
        id TEXT PRIMARY KEY,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest)=64
            AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        state TEXT NOT NULL CHECK (state IN ('released','revoked')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        UNIQUE(manifest_id, revision)
        );
INSERT INTO "artifact_custody_releases" VALUES('artifact_custody_release_artifact_manifest_441fb0c2f575ef75910675af3654b775_1','artifact_manifest_441fb0c2f575ef75910675af3654b775','43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc',1,'released','researcher may inspect private retained output','principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.498600Z');
CREATE TABLE artifact_dispositions (
        id TEXT PRIMARY KEY,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
        manifest_digest TEXT NOT NULL CHECK (
            length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
        ),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        disposition TEXT NOT NULL CHECK (disposition IN ('retain_quarantined', 'reject')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        UNIQUE(manifest_id, revision)
        );
INSERT INTO "artifact_dispositions" VALUES('artifact_disposition_artifact_manifest_441fb0c2f575ef75910675af3654b775_1','artifact_manifest_441fb0c2f575ef75910675af3654b775','43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc',1,'retain_quarantined','retain bounded output under quarantine','principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.492983Z');
CREATE TABLE artifact_lineage_edges (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        child_node_id TEXT NOT NULL REFERENCES artifact_lineage_nodes(id),
        parent_node_id TEXT NOT NULL REFERENCES artifact_lineage_nodes(id),
        relation TEXT NOT NULL CHECK (
            relation IN ('derived_from', 'trained_from', 'labels_from', 'evaluated_on')
        ),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 4000),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        CHECK (child_node_id <> parent_node_id),
        UNIQUE(child_node_id, parent_node_id, relation)
        );
CREATE TABLE artifact_lineage_nodes (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        kind TEXT NOT NULL CHECK (
            kind IN ('model_checkpoint', 'dataset', 'labels', 'evaluation', 'other')
        ),
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        label TEXT NOT NULL CHECK (length(label) BETWEEN 1 AND 200),
        origin TEXT NOT NULL CHECK (origin IN ('tessera_attempt', 'external_import')),
        artifact_id TEXT REFERENCES artifacts(id),
        result_checkpoint_id TEXT REFERENCES execution_result_checkpoints(id),
        producing_attempt_id TEXT REFERENCES research_experiment_admitted_attempts(id),
        operation_id TEXT REFERENCES external_operations(operation_id),
        framework TEXT CHECK (framework IS NULL OR length(framework) BETWEEN 1 AND 100),
        format TEXT CHECK (format IS NULL OR length(format) BETWEEN 1 AND 100),
        source_uri TEXT CHECK (source_uri IS NULL OR length(source_uri) BETWEEN 1 AND 2000),
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        CHECK (origin <> 'tessera_attempt' OR producing_attempt_id IS NOT NULL),
        CHECK (origin <> 'external_import' OR producing_attempt_id IS NULL),
        CHECK (kind <> 'model_checkpoint' OR (framework IS NOT NULL AND format IS NOT NULL)),
        UNIQUE(project_id, sha256)
        );
CREATE TABLE artifact_manifests (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id) UNIQUE,
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL,
        plan_digest TEXT NOT NULL,
        artifacts_json TEXT NOT NULL,
        total_bytes INTEGER NOT NULL CHECK (total_bytes >= 0 AND total_bytes <= 16777216),
        manifest_digest TEXT NOT NULL CHECK (
            length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
        ),
        created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status = 'quarantined'),
        classification TEXT NOT NULL CHECK (classification = 'unclassified'),
        late INTEGER NOT NULL CHECK (late IN (0, 1))
        );
INSERT INTO "artifact_manifests" VALUES('artifact_manifest_441fb0c2f575ef75910675af3654b775','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','grant_7787a24afa2f4948a6428db2dc0e110e','lease_71b2e54c24d040ebb9e0494eb6891fee','worker-worker-token',2,'94cb5622d57c29865de7b74891860b47','9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7','[{"id":"artifact_80bbef8fd2992c60467394b2bebaae1b","relative_path":"first.txt","sha256":"d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579","size":6},{"id":"artifact_cb220f89361ab592d4f3a7590b05a1ec","relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7}]',13,'43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc','2026-11-20T19:17:29.486940Z','quarantined','unclassified',0);
CREATE TABLE artifact_uploads (
        id TEXT PRIMARY KEY,
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0 AND size <= 16777216),
        received_bytes INTEGER NOT NULL DEFAULT 0 CHECK (
            received_bytes >= 0 AND received_bytes <= size
        ),
        staged_content BLOB,
        status TEXT NOT NULL CHECK (status IN ('uploading', 'completed')),
        created_at TEXT NOT NULL,
        completed_at TEXT,
        UNIQUE (lease_id, relative_path)
        );
INSERT INTO "artifact_uploads" VALUES('artifact_upload_cb220f89361ab592d4f3a7590b05a1ec','lease_71b2e54c24d040ebb9e0494eb6891fee','worker-worker-token',2,'94cb5622d57c29865de7b74891860b47','second.txt','59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395',7,7,NULL,'completed','2026-11-20T19:17:29.456541Z','2026-11-20T19:17:29.460809Z');
CREATE TABLE artifacts (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL,
        relative_path TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (
            length(sha256) = 64 AND sha256 NOT GLOB '*[^0-9a-f]*'
        ),
        size INTEGER NOT NULL CHECK (size >= 0),
        content BLOB NOT NULL,
        created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status = 'quarantined'),
        classification TEXT NOT NULL CHECK (classification = 'unclassified'),
        late INTEGER NOT NULL CHECK (late IN (0, 1)),
        UNIQUE (lease_id, relative_path)
        );
INSERT INTO "artifacts" VALUES('artifact_80bbef8fd2992c60467394b2bebaae1b','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','grant_7787a24afa2f4948a6428db2dc0e110e','lease_71b2e54c24d040ebb9e0494eb6891fee','worker-worker-token',2,'94cb5622d57c29865de7b74891860b47','first.txt','d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579',6,X'646972656374','2026-11-20T19:17:29.454223Z','quarantined','unclassified',0);
INSERT INTO "artifacts" VALUES('artifact_cb220f89361ab592d4f3a7590b05a1ec','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','grant_7787a24afa2f4948a6428db2dc0e110e','lease_71b2e54c24d040ebb9e0494eb6891fee','worker-worker-token',2,'94cb5622d57c29865de7b74891860b47','second.txt','59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395',7,X'6368756E6B6564','2026-11-20T19:17:29.460997Z','quarantined','unclassified',0);
CREATE TABLE astra_consumer_health (
        agent_principal_id TEXT PRIMARY KEY REFERENCES principals(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        heartbeat_at TEXT NOT NULL, last_outcome TEXT NOT NULL CHECK (
            last_outcome IN ('idle','claimed','completed','retrying')),
        updated_at TEXT NOT NULL);
CREATE TABLE astra_conversation_messages (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            conversation_revision INTEGER NOT NULL CHECK (conversation_revision >= 1),
            sender_kind TEXT NOT NULL CHECK (sender_kind IN ('human','coordinator')),
            body TEXT NOT NULL CHECK (length(body) <= 4000),
            turn_id TEXT, context_snapshot_id TEXT,
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            UNIQUE(project_id, conversation_revision)
        );
CREATE TABLE astra_coordinator_turns (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            input_message_id TEXT NOT NULL UNIQUE REFERENCES astra_conversation_messages(id),
            context_snapshot_id TEXT NOT NULL REFERENCES astra_selected_context_snapshots(id),
            state TEXT NOT NULL CHECK (state IN ('requested','claimed','completed','failed','cancelled')),
            revision INTEGER NOT NULL CHECK (revision >= 1),
            claimed_by_principal_id TEXT REFERENCES principals(id), claimed_at TEXT,
            completion_message_id TEXT UNIQUE REFERENCES astra_conversation_messages(id),
            proposal_id TEXT UNIQUE, error_code TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        , claim_expires_at TEXT, claim_attempts INTEGER NOT NULL DEFAULT 0);
CREATE TABLE astra_project_conversations (
            project_id TEXT PRIMARY KEY REFERENCES projects(id),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            revision INTEGER NOT NULL CHECK (revision >= 0),
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        );
CREATE TABLE astra_proposal_design_registrations (
                proposal_id TEXT PRIMARY KEY REFERENCES astra_proposals(id),
                org_id TEXT NOT NULL REFERENCES organizations(id),
                project_id TEXT NOT NULL REFERENCES projects(id),
                experiment_id TEXT NOT NULL UNIQUE REFERENCES research_experiments(id),
                design_revision INTEGER NOT NULL CHECK (design_revision = 1),
                context_digest TEXT NOT NULL,
                created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
            );
CREATE TABLE astra_proposals (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            conversation_revision INTEGER NOT NULL CHECK (conversation_revision >= 1),
            turn_id TEXT NOT NULL UNIQUE REFERENCES astra_coordinator_turns(id),
            proposal_type TEXT NOT NULL CHECK (proposal_type = 'human_review_needed'),
            body TEXT NOT NULL CHECK (length(body) <= 4000), recap TEXT NOT NULL CHECK (length(recap) <= 4000),
            status_json TEXT NOT NULL, provider_disabled INTEGER NOT NULL CHECK (provider_disabled = 1),
            authority_effect TEXT NOT NULL CHECK (authority_effect = 'none'), design_json TEXT, created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id));
CREATE TABLE astra_selected_context_snapshots (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            conversation_revision INTEGER NOT NULL CHECK (conversation_revision >= 1),
            selected_context_json TEXT NOT NULL, selected_context_digest TEXT NOT NULL,
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        );
CREATE TABLE astra_status_answers (
            id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussions(turn_id),
            binding_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussion_bindings(id),
            invocation_id TEXT NOT NULL UNIQUE REFERENCES astra_status_tool_invocations(id),
            org_id TEXT NOT NULL REFERENCES organizations(id), project_id TEXT NOT NULL REFERENCES projects(id),
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id), design_revision INTEGER NOT NULL,
            operation_ids_json TEXT NOT NULL, body TEXT NOT NULL CHECK (length(body)<=4000), body_digest TEXT NOT NULL,
            provenance_json TEXT NOT NULL,
            provider_disabled INTEGER NOT NULL CHECK (provider_disabled=1), created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision)
        );
CREATE TABLE astra_status_discussion_bindings (
            id TEXT PRIMARY KEY, turn_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussions(turn_id),
            org_id TEXT NOT NULL REFERENCES organizations(id), project_id TEXT NOT NULL REFERENCES projects(id),
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id), design_revision INTEGER NOT NULL,
            credential_id TEXT NOT NULL REFERENCES credentials(id), agent_principal_id TEXT NOT NULL REFERENCES principals(id),
            lease_expires_at TEXT NOT NULL, state TEXT NOT NULL CHECK (state IN ('claimed','tool_intent','tool_result','answered','uncertain','cancelled','expired')),
            revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision)
        );
CREATE TABLE astra_status_discussions (
            turn_id TEXT PRIMARY KEY REFERENCES astra_coordinator_turns(id),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
            design_revision INTEGER NOT NULL CHECK (design_revision >= 1),
            created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision)
        );
CREATE TABLE astra_status_tool_invocations (
            id TEXT PRIMARY KEY, binding_id TEXT NOT NULL UNIQUE REFERENCES astra_status_discussion_bindings(id),
            tool_name TEXT NOT NULL CHECK (tool_name='read_experiment_status'), input_json TEXT NOT NULL, input_digest TEXT NOT NULL,
            result_json TEXT, result_digest TEXT, provenance_json TEXT NOT NULL,
            outcome TEXT NOT NULL CHECK (outcome IN ('intent','recorded','uncertain','cancelled')),
            created_at TEXT NOT NULL, observed_at TEXT, created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        );
CREATE TABLE capability_operations (
        id TEXT PRIMARY KEY,
        policy_id TEXT NOT NULL REFERENCES capability_policies(id),
        action_id TEXT NOT NULL,
        worker_id TEXT NOT NULL REFERENCES principals(id),
        binding_json TEXT NOT NULL,
        deadline TEXT NOT NULL,
        receipt_json TEXT,
        created_at TEXT NOT NULL,
        UNIQUE(policy_id, action_id)
    );
CREATE TABLE capability_policies (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        plan_revision INTEGER NOT NULL,
        plan_digest TEXT NOT NULL,
        declaration_json TEXT NOT NULL,
        declaration_digest TEXT NOT NULL,
        requested_by TEXT NOT NULL REFERENCES principals(id),
        approved_by TEXT REFERENCES principals(id),
        expires_at TEXT,
        status TEXT NOT NULL CHECK(status IN ('requested','approved','revoked')),
        revision INTEGER NOT NULL,
        created_at TEXT NOT NULL
    );
CREATE TABLE control_recovery (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        state TEXT NOT NULL CHECK (state IN ('active', 'held_restore')),
        incarnation TEXT NOT NULL,
        restore_id TEXT,
        source_backup_digest TEXT,
        event_watermark INTEGER,
        restored_at TEXT
        );
INSERT INTO "control_recovery" VALUES(1,'active','938f4cdaea04471592bdd11c790c78e1',NULL,NULL,NULL,NULL);
CREATE TABLE credential_access_leases (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        execution_lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        secret_ref TEXT NOT NULL CHECK (length(secret_ref) BETWEEN 1 AND 200),
        audience TEXT NOT NULL CHECK (length(audience) BETWEEN 1 AND 200),
        purpose TEXT NOT NULL CHECK (length(purpose) BETWEEN 1 AND 200),
        scope_json TEXT NOT NULL CHECK (length(scope_json) BETWEEN 2 AND 8192),
        expires_at TEXT NOT NULL,
        revoked_at TEXT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        revoked_by_principal_id TEXT REFERENCES principals(id),
        UNIQUE(execution_lease_id, secret_ref, audience, purpose)
        );
CREATE TABLE credentials (
            id TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
            kind TEXT NOT NULL CHECK (kind IN ('local_owner', 'human_session', 'agent', 'worker')),
            audience TEXT NOT NULL CHECK (audience IN ('control', 'worker')),
            project_id TEXT REFERENCES projects(id),
            parent_id TEXT REFERENCES credentials(id),
            expires_at TEXT,
            revoked_at TEXT,
            created_at TEXT NOT NULL,
            created_by_principal_id TEXT REFERENCES principals(id)
        );
INSERT INTO "credentials" VALUES('credential_7dcee7be7ee5440ca35ca4c86b637077','principal_12227679343446a2959e088ba1daf770','local_owner','control',NULL,NULL,NULL,NULL,'2026-11-20T19:17:29.375464Z','principal_12227679343446a2959e088ba1daf770');
INSERT INTO "credentials" VALUES('credential-worker-token','worker-worker-token','worker','worker','project_ba8781fd818b4e20b2189879f3967aec','credential_7dcee7be7ee5440ca35ca4c86b637077','2026-11-20T20:17:29.389435Z',NULL,'2026-11-20T19:17:29.389435Z','principal_12227679343446a2959e088ba1daf770');
CREATE TABLE dataset_ingestion_proposals (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(id),
            intended_use TEXT NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('proposed','approved','rejected')),
            revision INTEGER NOT NULL, rationale TEXT, reviewed_at TEXT,
            created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            reviewed_by_principal_id TEXT REFERENCES principals(id)
        );
CREATE TABLE dataset_snapshot_sources (
            snapshot_id TEXT NOT NULL REFERENCES dataset_snapshots(id),
            source_id TEXT NOT NULL REFERENCES research_sources(id),
            expected_source_revision INTEGER NOT NULL CHECK (expected_source_revision >= 1),
            expected_content_digest TEXT NOT NULL CHECK (
                length(expected_content_digest)=64
                AND expected_content_digest NOT GLOB '*[^0-9a-f]*'
            ), license_expression TEXT NOT NULL,
            license_status TEXT NOT NULL CHECK (license_status IN ('declared','unsupported')),
            provenance_kind TEXT NOT NULL,
            provenance_status TEXT NOT NULL CHECK (provenance_status IN ('declared','unsupported')),
            PRIMARY KEY(snapshot_id,source_id)
        );
CREATE TABLE dataset_snapshots (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            name TEXT NOT NULL, snapshot_digest TEXT NOT NULL CHECK (
                length(snapshot_digest)=64 AND snapshot_digest NOT GLOB '*[^0-9a-f]*'
            ), schema_json TEXT NOT NULL, schema_digest TEXT NOT NULL CHECK (
                length(schema_digest)=64 AND schema_digest NOT GLOB '*[^0-9a-f]*'
            ), record_count INTEGER NOT NULL CHECK (record_count >= 0),
            validation_summary_json TEXT NOT NULL, split_strategy_json TEXT NOT NULL,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            UNIQUE(study_id, snapshot_digest)
        );
CREATE TABLE decision_requests (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id), task_id TEXT REFERENCES tasks(id),
            kind TEXT NOT NULL CHECK (kind IN ('direction','spending','access','publication')),
            scope_json TEXT NOT NULL, scope_digest TEXT NOT NULL, expected_task_revision INTEGER,
            reviewer_principal_id TEXT NOT NULL REFERENCES principals(id),
            requester_principal_id TEXT NOT NULL REFERENCES principals(id),
            status TEXT NOT NULL CHECK (status IN ('pending','approved','rejected','expired')),
            revision INTEGER NOT NULL, expires_at TEXT NOT NULL,
            priority INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
            created_at TEXT NOT NULL, resolved_at TEXT,
            resolved_by_principal_id TEXT REFERENCES principals(id), rationale TEXT
        );
CREATE TABLE events (
            cursor INTEGER PRIMARY KEY AUTOINCREMENT,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT REFERENCES tasks(id),
            event_type TEXT NOT NULL,
            resource_revision INTEGER NOT NULL,
            actor_principal_id TEXT NOT NULL REFERENCES principals(id),
            created_at TEXT NOT NULL,
            correlation_id TEXT,
            causation_id TEXT,
            payload_json TEXT NOT NULL
        );
INSERT INTO "events" VALUES(1,'org_local','project_ba8781fd818b4e20b2189879f3967aec',NULL,'project.created',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.389115Z','command_ca245bb38aac4dac94a797aa96c4fb1e','command_ca245bb38aac4dac94a797aa96c4fb1e','{"name":"Pilot"}');
INSERT INTO "events" VALUES(2,'org_local','project_ba8781fd818b4e20b2189879f3967aec',NULL,'workspace.snapshot_adopted',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.404507Z','command_f1c620b6ee41442392a3a4a8366fc923','command_f1c620b6ee41442392a3a4a8366fc923','{"classification":"unclassified","data_class":{"actor_principal_id":null,"data_class":"private_derived","decision_class":null,"license":null,"origin":"default","recorded_at":"2026-11-20T19:17:29.404293Z","source":null},"digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","file_count":1,"provenance":"imported_unverified","snapshot_id":"snapshot_f128bc3275b34924b75ec6951e10f5d1","total_bytes":15}');
INSERT INTO "events" VALUES(3,'org_local','project_ba8781fd818b4e20b2189879f3967aec',NULL,'workspace.snapshot_released',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.411239Z','command_38457311453347d1861b0f008376cd62','command_38457311453347d1861b0f008376cd62','{"automation":true,"automation_label":"fixture","classification":"public","credential_kind":"local_owner","decision_class":"owner_automation_labelled","digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","expires_at":"2026-11-20T19:47:29.404889+00:00","release_id":"release_91eeb00752b747cd9ec3df33cf9da7a7","snapshot_id":"snapshot_f128bc3275b34924b75ec6951e10f5d1","worker_principal_id":"worker-worker-token"}');
INSERT INTO "events" VALUES(4,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','task.created',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.417190Z','command_f9d0f7f8226e44cf9ce7360bad698477','command_f9d0f7f8226e44cf9ce7360bad698477','{"plan_digest":"bd944782a62c84697e384d71611dd7693b23ea08f50a6465d0080a36c64e6da0","status":"draft","title":"First"}');
INSERT INTO "events" VALUES(5,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','task.plan_revised',2,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.419427Z','command_f0fd73c42ebe44eb91260e41e40fe24b','command_f0fd73c42ebe44eb91260e41e40fe24b','{"invalidated_approval":false,"plan_digest":"0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611","status":"draft"}');
INSERT INTO "events" VALUES(6,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','task.approved',3,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.421594Z','command_1c6ca9793a2e418fab3c3980b79b7d8b','command_1c6ca9793a2e418fab3c3980b79b7d8b','{"approval_scope":"research_plan_only","approved_plan_digest":"0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611","approved_plan_revision":2,"approving_principal_id":"principal_12227679343446a2959e088ba1daf770","automation":true,"automation_label":"fixture","credential_kind":"local_owner","decision_class":"owner_automation_labelled","execution_granted":false,"status":"approved"}');
INSERT INTO "events" VALUES(7,'org_local','project_ba8781fd818b4e20b2189879f3967aec',NULL,'execution.limits_configured',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.427516Z','command_9bcb62a02d0d498f9eade1498f7a2266','command_9bcb62a02d0d498f9eade1498f7a2266','{"currency":"USD","max_concurrency":1,"monetary_limit_cents":100,"operational_only":true}');
INSERT INTO "events" VALUES(8,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','task.plan_revised',4,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.436862Z','command_5377dd67e8db403493f366af3cb894d8','command_5377dd67e8db403493f366af3cb894d8','{"invalidated_approval":true,"plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","status":"draft"}');
INSERT INTO "events" VALUES(9,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','task.approved',5,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.442228Z','command_d80c9593d5b44246abde3bd216373585','command_d80c9593d5b44246abde3bd216373585','{"approval_scope":"research_plan_only","approved_plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","approved_plan_revision":4,"approving_principal_id":"principal_12227679343446a2959e088ba1daf770","automation":true,"automation_label":"fixture","credential_kind":"local_owner","decision_class":"owner_automation_labelled","execution_granted":false,"status":"approved"}');
INSERT INTO "events" VALUES(10,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','execution.grant_issued',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.444769Z','command_8c8d6c1c671f417bae6f45829a63cf9a','command_8c8d6c1c671f417bae6f45829a63cf9a','{"automation":true,"automation_label":"fixture","credential_kind":"local_owner","currency":"USD","decision_class":"owner_automation_labelled","epoch":1,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","max_exposure_cents":60,"operational_only":true,"pinned_skill_digests":[],"snapshot_release_id":"release_91eeb00752b747cd9ec3df33cf9da7a7","worker_principal_id":"worker-worker-token"}');
INSERT INTO "events" VALUES(11,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','execution.lease_acquired',2,'worker-worker-token','2026-11-20T19:17:29.448051Z','command_7544c0af001442c7b32ce7b2d5c71cb1','command_7544c0af001442c7b32ce7b2d5c71cb1','{"epoch":2,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","operational_only":true}');
INSERT INTO "events" VALUES(12,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','worker.artifact_quarantined',1,'worker-worker-token','2026-11-20T19:17:29.454323Z','command_e72fcdfecf9046edbef363238932849b','command_e72fcdfecf9046edbef363238932849b','{"classification":"unclassified","created_at":"2026-11-20T19:17:29.454223Z","epoch":2,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","id":"artifact_80bbef8fd2992c60467394b2bebaae1b","late":false,"lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","operation_id":"94cb5622d57c29865de7b74891860b47","project_id":"project_ba8781fd818b4e20b2189879f3967aec","relative_path":"first.txt","sha256":"d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579","size":6,"status":"quarantined","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca","worker_principal_id":"worker-worker-token"}');
INSERT INTO "events" VALUES(13,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','worker.artifact_upload_begun',1,'worker-worker-token','2026-11-20T19:17:29.456610Z','command_689fb9677280438daea53235263401dd','command_689fb9677280438daea53235263401dd','{"id":"artifact_upload_cb220f89361ab592d4f3a7590b05a1ec","lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","received_bytes":0,"relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"status":"uploading"}');
INSERT INTO "events" VALUES(14,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','worker.artifact_upload_progressed',1,'worker-worker-token','2026-11-20T19:17:29.458742Z','command_864ad2a1af80453cad69d73c47ccf71e','command_864ad2a1af80453cad69d73c47ccf71e','{"id":"artifact_upload_cb220f89361ab592d4f3a7590b05a1ec","lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","received_bytes":7,"relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"status":"uploading"}');
INSERT INTO "events" VALUES(15,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','worker.artifact_quarantined',1,'worker-worker-token','2026-11-20T19:17:29.461061Z','command_53c84232c66d4dab88b9571f6af79ed3','command_53c84232c66d4dab88b9571f6af79ed3','{"classification":"unclassified","created_at":"2026-11-20T19:17:29.460997Z","epoch":2,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","id":"artifact_cb220f89361ab592d4f3a7590b05a1ec","late":false,"lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","operation_id":"94cb5622d57c29865de7b74891860b47","project_id":"project_ba8781fd818b4e20b2189879f3967aec","relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"status":"quarantined","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca","worker_principal_id":"worker-worker-token"}');
INSERT INTO "events" VALUES(16,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','worker.project_artifacts_committed',1,'worker-worker-token','2026-11-20T19:17:29.479032Z','command_86425e2d7e6c4190b8ac13f4de2dff0f','command_86425e2d7e6c4190b8ac13f4de2dff0f','{"artifact_ids":["artifact_80bbef8fd2992c60467394b2bebaae1b","artifact_cb220f89361ab592d4f3a7590b05a1ec"],"explicit_zero_output":false,"lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee"}');
INSERT INTO "events" VALUES(17,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','worker.artifact_manifest_quarantined',1,'worker-worker-token','2026-11-20T19:17:29.487032Z','command_5a77f8546be843c1a4e4bf272c7799e0','command_5a77f8546be843c1a4e4bf272c7799e0','{"artifact_ids":["artifact_80bbef8fd2992c60467394b2bebaae1b","artifact_cb220f89361ab592d4f3a7590b05a1ec"],"id":"artifact_manifest_441fb0c2f575ef75910675af3654b775","lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","manifest_digest":"43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc"}');
INSERT INTO "events" VALUES(18,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','human.artifact_disposition_recorded',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.493061Z','command_b72a03d18de940339d461e04fb5a733c','command_b72a03d18de940339d461e04fb5a733c','{"disposition":"retain_quarantined","manifest_digest":"43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc","manifest_id":"artifact_manifest_441fb0c2f575ef75910675af3654b775"}');
INSERT INTO "events" VALUES(19,'org_local','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca','human.artifact_custody_released',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.498660Z','command_83f2f9a3913648dfb60355c0a683a1bf','command_83f2f9a3913648dfb60355c0a683a1bf','{"manifest_digest":"43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc","manifest_id":"artifact_manifest_441fb0c2f575ef75910675af3654b775"}');
INSERT INTO "events" VALUES(20,'org_local','project_ba8781fd818b4e20b2189879f3967aec',NULL,'workspace.snapshot_adopted',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.509335Z','command_f9587042ed324d5eb13d9e900e22c364','command_f9587042ed324d5eb13d9e900e22c364','{"chunk_count":1,"chunked":true,"classification":"unclassified","data_class":{"actor_principal_id":null,"data_class":"private_derived","decision_class":null,"license":null,"origin":"default","recorded_at":"2026-11-20T19:17:29.509169Z","source":null},"digest":"234602367fcc57299510b5f8340b9dc86f0a1d1876650b02ae86f35b96b1c9f4","file_count":1,"provenance":"imported_unverified","snapshot_id":"snapshot_aa8f35d72c4948b3866b162b45f07574","total_bytes":6}');
INSERT INTO "events" VALUES(21,'org_local','project_ba8781fd818b4e20b2189879f3967aec',NULL,'workspace.snapshot_adopted',1,'principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.516109Z','command_48fb6b823c4b492bb899ab5d76545dec','command_48fb6b823c4b492bb899ab5d76545dec','{"chunk_count":1,"chunked":true,"classification":"unclassified","data_class":{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","data_class":"public","decision_class":"owner_automation_labelled","license":"CC0-1.0","origin":"human_declared","recorded_at":"2026-11-20T19:17:29.511507Z","source":"https://example.org/data"},"digest":"15618551f69b87c9ee4f4ddd5a9f9f5ece77aec6dc5c114721520a64710bfb70","file_count":1,"provenance":"imported_unverified","snapshot_id":"snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8","total_bytes":2}');
CREATE TABLE execution_grant_skills (
        grant_id TEXT NOT NULL REFERENCES execution_grants(id), digest TEXT NOT NULL,
        skill_release_id TEXT NOT NULL REFERENCES skill_releases(id),
        expires_at TEXT NOT NULL, PRIMARY KEY(grant_id, digest));
CREATE TABLE execution_grants (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        task_revision INTEGER NOT NULL CHECK (task_revision >= 1),
        plan_digest TEXT NOT NULL,
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        snapshot_digest TEXT NOT NULL,
        snapshot_release_id TEXT NOT NULL REFERENCES snapshot_releases(id),
        snapshot_release_expires_at TEXT NOT NULL,
        pinned_image TEXT NOT NULL,
        currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
        max_exposure_cents INTEGER NOT NULL CHECK (max_exposure_cents >= 0),
        acknowledges_unmeasured_infrastructure INTEGER NOT NULL CHECK
            (acknowledges_unmeasured_infrastructure IN (0, 1)),
        status TEXT NOT NULL CHECK (status IN ('active', 'cancel_requested', 'reconciled')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        reconciled_at TEXT,
        reconciled_by_principal_id TEXT REFERENCES principals(id),
        settlement_cents INTEGER CHECK (settlement_cents >= 0)
        );
INSERT INTO "execution_grants" VALUES('grant_7787a24afa2f4948a6428db2dc0e110e','project_ba8781fd818b4e20b2189879f3967aec','task_d63915ceaaac47c4a4b8f082be1b5bca',5,'9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7','worker-worker-token','snapshot_f128bc3275b34924b75ec6951e10f5d1','1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500','release_91eeb00752b747cd9ec3df33cf9da7a7','2026-11-20T19:47:29.404889Z','python@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','USD',60,1,'active',1,2,'2026-11-20T19:37:29.442541Z','2026-11-20T19:17:29.444600Z','2026-11-20T19:17:29.447935Z','principal_12227679343446a2959e088ba1daf770',NULL,NULL,NULL);
CREATE TABLE execution_leases (
        id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        issued_at TEXT NOT NULL, expires_at TEXT NOT NULL, hard_expires_at TEXT NOT NULL,
        renewed_at TEXT NOT NULL
        );
INSERT INTO "execution_leases" VALUES('lease_71b2e54c24d040ebb9e0494eb6891fee','grant_7787a24afa2f4948a6428db2dc0e110e','worker-worker-token',2,'2026-11-20T19:17:29.447935Z','2026-11-20T19:17:59.447935Z','2026-11-20T19:18:29.447935Z','2026-11-20T19:17:29.447935Z');
CREATE TABLE execution_observations (
        id TEXT PRIMARY KEY,
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        desired_state TEXT NOT NULL CHECK (desired_state IN ('admitted', 'cancel_requested')),
        observed_state TEXT NOT NULL CHECK (observed_state IN ('running', 'exited', 'unknown')),
        observed_at TEXT NOT NULL, provider_id TEXT,
        termination_confirmed INTEGER NOT NULL CHECK (termination_confirmed IN (0, 1)),
        recorded_at TEXT NOT NULL
        , operation_id TEXT);
CREATE TABLE execution_observed_result_acceptances (
        id TEXT PRIMARY KEY,
        checkpoint_id TEXT NOT NULL REFERENCES execution_result_checkpoints(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        action TEXT NOT NULL CHECK (action IN ('accept', 'reject', 'retract')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        checkpoint_digest TEXT NOT NULL CHECK (length(checkpoint_digest) = 64),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest) = 64),
        validation_digest TEXT NOT NULL CHECK (length(validation_digest) = 64),
        receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
        grant_revision INTEGER NOT NULL CHECK (grant_revision >= 1),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        operation_id TEXT NOT NULL REFERENCES execution_run_receipts(operation_id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        provider_kind TEXT NOT NULL CHECK (
            provider_kind IN ('local_docker_cpu', 'remote_kubernetes', 'remote_salad')
        ),
        exit_code INTEGER NOT NULL,
        scientific_validity TEXT NOT NULL CHECK (scientific_validity = 'not_assessed'),
        provider_success TEXT NOT NULL CHECK (provider_success = 'not_established'),
        artifact_release TEXT NOT NULL CHECK (artifact_release = 'not_performed'),
        evidence_label TEXT NOT NULL CHECK (evidence_label = 'provider_observed_unassessed'),
        receipt_evidence TEXT NOT NULL CHECK (receipt_evidence = 'observed_provider_execution'),
        UNIQUE(checkpoint_id, revision)
        );
CREATE TABLE execution_project_limits (
        project_id TEXT PRIMARY KEY REFERENCES projects(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        currency TEXT NOT NULL CHECK (currency GLOB '[A-Z][A-Z][A-Z]'),
        monetary_limit_cents INTEGER NOT NULL CHECK (monetary_limit_cents >= 0),
        max_concurrency INTEGER NOT NULL CHECK (max_concurrency BETWEEN 1 AND 8),
        held_cents INTEGER NOT NULL DEFAULT 0 CHECK (held_cents >= 0),
        settled_cents INTEGER NOT NULL DEFAULT 0 CHECK (settled_cents >= 0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
INSERT INTO "execution_project_limits" VALUES('project_ba8781fd818b4e20b2189879f3967aec',1,'USD',100,1,60,0,'2026-11-20T19:17:29.427462Z','2026-11-20T19:17:29.444600Z');
CREATE TABLE execution_recovery_observations (
        id TEXT PRIMARY KEY,
        recovery_id TEXT NOT NULL REFERENCES execution_recovery_reconciliations(id),
        observer_principal_id TEXT NOT NULL REFERENCES principals(id),
        action TEXT NOT NULL CHECK (action IN ('lookup','cancel_lookup')),
        evidence_reference TEXT NOT NULL CHECK (length(evidence_reference) BETWEEN 1 AND 200),
        evidence_digest TEXT NOT NULL CHECK (length(evidence_digest)=64
            AND evidence_digest NOT GLOB '*[^0-9a-f]*'),
        provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 200),
        observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown','running','exited')),
        observed_at TEXT NOT NULL, termination_confirmed INTEGER NOT NULL CHECK
            (termination_confirmed IN (0,1)), recorded_at TEXT NOT NULL
        );
CREATE TABLE execution_recovery_reconciliations (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        operation_id TEXT NOT NULL UNIQUE REFERENCES external_operations(operation_id),
        original_worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        identity_loss_reason TEXT NOT NULL CHECK (identity_loss_reason IN
            ('credential_revoked','credential_expired','principal_revoked','worker_retired','registry_missing')),
        state TEXT NOT NULL CHECK (state IN ('identity_lost','reconciliation_required',
            'cancel_requested','independently_observed','terminal_confirmed','unresolved')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        cancellation_requested INTEGER NOT NULL DEFAULT 0 CHECK (cancellation_requested IN (0,1)),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        closed_at TEXT, closed_by_principal_id TEXT REFERENCES principals(id)
        );
CREATE TABLE execution_result_acceptances (
        id TEXT PRIMARY KEY,
        checkpoint_id TEXT NOT NULL REFERENCES execution_result_checkpoints(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        action TEXT NOT NULL CHECK (action IN ('accept', 'reject', 'retract')),
        rationale TEXT NOT NULL CHECK (length(rationale) BETWEEN 1 AND 8000),
        checkpoint_digest TEXT NOT NULL CHECK (length(checkpoint_digest) = 64),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest) = 64),
        validation_digest TEXT NOT NULL CHECK (length(validation_digest) = 64),
        receipt_digest TEXT NOT NULL CHECK (length(receipt_digest) = 64),
        grant_revision INTEGER NOT NULL CHECK (grant_revision >= 1),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        operation_id TEXT NOT NULL CHECK (length(operation_id) = 32),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL,
        scientific_validity TEXT NOT NULL CHECK (scientific_validity = 'not_assessed'),
        provider_success TEXT NOT NULL CHECK (provider_success = 'not_established'),
        artifact_release TEXT NOT NULL CHECK (artifact_release = 'not_performed'),
        evidence_label TEXT NOT NULL CHECK (evidence_label = 'development_synthetic_only'),
        receipt_evidence TEXT NOT NULL CHECK (receipt_evidence = 'fake_provider_development_only'),
        UNIQUE(checkpoint_id, revision)
        );
CREATE TABLE execution_result_checkpoints (
        id TEXT PRIMARY KEY,
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        operation_id TEXT NOT NULL,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id) UNIQUE,
        manifest_digest TEXT NOT NULL CHECK (
            length(manifest_digest) = 64 AND manifest_digest NOT GLOB '*[^0-9a-f]*'
        ),
        grant_revision INTEGER NOT NULL CHECK (grant_revision >= 1),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        validation_contract TEXT NOT NULL CHECK (
            validation_contract = 'synthetic-declared-output-v1'
        ),
        validation_json TEXT NOT NULL,
        validation_digest TEXT NOT NULL CHECK (
            length(validation_digest) = 64 AND validation_digest NOT GLOB '*[^0-9a-f]*'
        ),
        commit_digest TEXT NOT NULL CHECK (
            length(commit_digest) = 64 AND commit_digest NOT GLOB '*[^0-9a-f]*'
        ),
        committed_at TEXT NOT NULL,
        committed_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        state TEXT NOT NULL CHECK (state = 'committed_quarantined')
        );
CREATE TABLE execution_run_receipts (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        provider_kind TEXT NOT NULL CHECK (
            provider_kind IN ('local_docker_cpu', 'remote_kubernetes', 'remote_salad')
        ),
        receipt_evidence TEXT NOT NULL CHECK (receipt_evidence = 'observed_provider_execution'),
        adapter_version TEXT NOT NULL CHECK (length(adapter_version) BETWEEN 1 AND 100),
        provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 200),
        command_digest TEXT NOT NULL CHECK (
            length(command_digest) = 64 AND command_digest NOT GLOB '*[^0-9a-f]*'
        ),
        exit_code INTEGER NOT NULL CHECK (exit_code BETWEEN -2147483648 AND 2147483647),
        started_at TEXT NOT NULL,
        finished_at TEXT NOT NULL,
        duration_ms INTEGER NOT NULL CHECK (duration_ms >= 0),
        output_digest TEXT NOT NULL CHECK (
            length(output_digest) = 64 AND output_digest NOT GLOB '*[^0-9a-f]*'
        ),
        receipt_payload_json TEXT NOT NULL CHECK (length(receipt_payload_json) <= 65536),
        receipt_digest TEXT NOT NULL CHECK (
            length(receipt_digest) = 64 AND receipt_digest NOT GLOB '*[^0-9a-f]*'
        ),
        provider_success TEXT NOT NULL CHECK (provider_success = 'not_established'),
        scientific_validity TEXT NOT NULL CHECK (scientific_validity = 'not_assessed'),
        created_at TEXT NOT NULL,
        CHECK (finished_at >= started_at),
        UNIQUE(provider_kind, adapter_version, provider_id)
        );
CREATE TABLE execution_runtime_packages (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        package_digest TEXT NOT NULL CHECK (
            length(package_digest)=64 AND package_digest NOT GLOB '*[^0-9a-f]*'),
        package_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE execution_task_epochs (
        task_id TEXT PRIMARY KEY REFERENCES tasks(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1)
        );
INSERT INTO "execution_task_epochs" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca',2);
CREATE TABLE experiment_artifact_evidence_references (
        id TEXT PRIMARY KEY, artifact_id TEXT NOT NULL REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        measurement_id TEXT REFERENCES research_attempt_measurements(id),
        reference_kind TEXT NOT NULL CHECK (reference_kind IN ('attempt_artifact','measurement_artifact')),
        created_at TEXT NOT NULL,
        CHECK ((reference_kind='attempt_artifact' AND measurement_id IS NULL) OR
               (reference_kind='measurement_artifact' AND measurement_id IS NOT NULL))
        );
CREATE TABLE experiment_budget_bound_resolutions (
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        revision INTEGER NOT NULL CHECK(revision>=2),
        field TEXT NOT NULL CHECK(field IN ('prior_settled_cents','prior_pending_cents',
            'ancillary_cents','shutdown_cents')),
        amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer'
            AND amount_cents BETWEEN 0 AND 9007199254740991),
        evidence_digest TEXT NOT NULL, source_reference TEXT NOT NULL,
        resolved_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(experiment_id,field));
CREATE TABLE experiment_budget_entries (
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        delivery_id TEXT NOT NULL, request_digest TEXT NOT NULL,
        admitted_attempt_id TEXT REFERENCES experiment_budget_reservations(admitted_attempt_id),
        category TEXT NOT NULL CHECK(category IN ('compute','prior','ancillary','shutdown')),
        amount_cents INTEGER NOT NULL CHECK(typeof(amount_cents)='integer' AND amount_cents BETWEEN 0 AND 9007199254740991),
        evidence_digest TEXT NOT NULL, source_reference TEXT NOT NULL,
        recorded_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(experiment_id,delivery_id));
CREATE TABLE experiment_budget_reservations (
        admitted_attempt_id TEXT PRIMARY KEY REFERENCES research_experiment_admitted_attempts(id),
        experiment_id TEXT NOT NULL REFERENCES experiment_budgets(experiment_id),
        grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
        authorization_revision INTEGER NOT NULL CHECK(authorization_revision>=1),
        original_cents INTEGER NOT NULL CHECK(typeof(original_cents)='integer' AND original_cents BETWEEN 0 AND 9007199254740991),
        held_cents INTEGER NOT NULL CHECK(typeof(held_cents)='integer' AND held_cents BETWEEN 0 AND 9007199254740991),
        settled_cents INTEGER NOT NULL CHECK(typeof(settled_cents)='integer' AND settled_cents BETWEEN 0 AND 9007199254740991),
        exposure_known INTEGER NOT NULL CHECK(exposure_known IN (0,1)),
        revision INTEGER NOT NULL CHECK(revision>=1));
CREATE TABLE experiment_budgets (
        experiment_id TEXT PRIMARY KEY REFERENCES research_experiments(id),
        ceiling_cents INTEGER NOT NULL CHECK(typeof(ceiling_cents)='integer'
            AND ceiling_cents BETWEEN 1 AND 5000),
        authorization_revision INTEGER NOT NULL CHECK(authorization_revision>=1),
        revision INTEGER NOT NULL CHECK(revision>=1),
        currency TEXT NOT NULL CHECK(currency='USD'),
        state TEXT NOT NULL CHECK(state IN ('active','fenced','closed')),
        expires_at TEXT NOT NULL, evidence_digest TEXT NOT NULL,
        source_reference TEXT NOT NULL, created_at TEXT NOT NULL,
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        prior_settled_cents INTEGER CHECK(prior_settled_cents IS NULL OR (typeof(prior_settled_cents)='integer' AND prior_settled_cents BETWEEN 0 AND 9007199254740991)),
        prior_pending_cents INTEGER CHECK(prior_pending_cents IS NULL OR (typeof(prior_pending_cents)='integer' AND prior_pending_cents BETWEEN 0 AND 9007199254740991)),
        ancillary_cents INTEGER CHECK(ancillary_cents IS NULL OR (typeof(ancillary_cents)='integer' AND ancillary_cents BETWEEN 0 AND 9007199254740991)),
        shutdown_cents INTEGER CHECK(shutdown_cents IS NULL OR (typeof(shutdown_cents)='integer' AND shutdown_cents BETWEEN 0 AND 9007199254740991)));
CREATE TABLE external_agent_contexts (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id), destination TEXT NOT NULL,
        payload_json TEXT NOT NULL, created_by TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL);
CREATE TABLE external_agent_promotions (
        proposal_id TEXT PRIMARY KEY REFERENCES external_agent_proposals(id),
        task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),
        promoted_by TEXT NOT NULL REFERENCES principals(id), created_at TEXT NOT NULL);
CREATE TABLE external_agent_proposals (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        credential_id TEXT NOT NULL REFERENCES credentials(id),
        submitted_by TEXT NOT NULL REFERENCES principals(id), destination TEXT NOT NULL,
        digest TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE external_agent_scopes (
        credential_id TEXT PRIMARY KEY REFERENCES credentials(id),
        actions_json TEXT NOT NULL, destination TEXT NOT NULL);
CREATE TABLE external_operation_receipts (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        provider_kind TEXT NOT NULL CHECK (provider_kind = 'fake'),
        provider_version TEXT NOT NULL CHECK (length(provider_version) BETWEEN 1 AND 100),
        provider_id TEXT NOT NULL CHECK (length(provider_id) BETWEEN 1 AND 200),
        receipt_payload_json TEXT NOT NULL CHECK (length(receipt_payload_json) <= 65536),
        receipt_digest TEXT NOT NULL CHECK (
            length(receipt_digest) = 64 AND receipt_digest NOT GLOB '*[^0-9a-f]*'
        ),
        observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown', 'running', 'exited')),
        observed_at TEXT NOT NULL,
        observation_sequence INTEGER NOT NULL DEFAULT 1 CHECK (observation_sequence >= 1),
        termination_confirmed INTEGER NOT NULL CHECK (termination_confirmed IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(provider_kind, provider_version, provider_id)
        );
CREATE TABLE external_operations (
        operation_id TEXT PRIMARY KEY CHECK (
            length(operation_id) = 32 AND operation_id NOT GLOB '*[^0-9a-f]*'
        ),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        package_digest TEXT NOT NULL CHECK (
            length(package_digest) = 64 AND package_digest NOT GLOB '*[^0-9a-f]*'
        ),
        adapter_version TEXT NOT NULL,
        profile_version TEXT NOT NULL,
        docker_lookup_key TEXT NOT NULL UNIQUE,
        request_digest TEXT NOT NULL CHECK (
            length(request_digest) = 64 AND request_digest NOT GLOB '*[^0-9a-f]*'
        ),
        retry_class TEXT NOT NULL CHECK (retry_class = 'manual_reconciliation'),
        desired_state TEXT NOT NULL CHECK (desired_state IN ('admitted', 'cancel_requested')),
         observed_state TEXT NOT NULL CHECK (observed_state IN ('unknown', 'running', 'exited')),
          provider_id TEXT,
          created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
        , experiment_attempt_id TEXT, experiment_id TEXT, experiment_design_revision INTEGER, experiment_variant_id TEXT);
CREATE TABLE gate_attempt_split_bindings (
        attempt_id TEXT PRIMARY KEY REFERENCES research_experiment_admitted_attempts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        membership_digest TEXT NOT NULL CHECK (length(membership_digest)=64 AND membership_digest NOT GLOB '*[^0-9a-f]*'),
        attempt_state_at_binding TEXT NOT NULL CHECK (attempt_state_at_binding='admitted'),
        bound_at TEXT NOT NULL,
        bound_by_principal_id TEXT NOT NULL REFERENCES principals(id), seed INTEGER CHECK (seed IS NULL OR seed >= 0), metrics_json TEXT);
CREATE TABLE gate_leakage_audits (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        kind TEXT NOT NULL CHECK (kind IN ('within_lock','cross_set')),
        reference_lock_id TEXT REFERENCES gate_split_locks(id),
        eval_partitions_json TEXT NOT NULL, reference_partitions_json TEXT NOT NULL,
        key_overlap_count INTEGER NOT NULL CHECK (key_overlap_count >= 0),
        group_overlap_count INTEGER NOT NULL CHECK (group_overlap_count >= 0),
        overlap_sample_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('clean','overlap_detected')),
        audit_digest TEXT NOT NULL CHECK (length(audit_digest)=64 AND audit_digest NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((kind='within_lock') = (reference_lock_id IS NULL)),
        CHECK ((status='clean') = (key_overlap_count=0 AND group_overlap_count=0)));
CREATE TABLE gate_metric_records (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        attempt_id TEXT NOT NULL
            REFERENCES gate_attempt_split_bindings(attempt_id),
        measurement_id TEXT NOT NULL UNIQUE
            REFERENCES research_admitted_attempt_measurements(id),
        measurement_source TEXT NOT NULL
            CHECK (measurement_source IN ('human_entered','derived')),
        metric TEXT NOT NULL, unit TEXT NOT NULL,
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL,
        membership_digest TEXT NOT NULL CHECK (length(membership_digest)=64 AND membership_digest NOT GLOB '*[^0-9a-f]*'),
        sample_count INTEGER NOT NULL CHECK (sample_count >= 1),
        seed INTEGER NOT NULL CHECK (seed >= 0),
        checkpoint_sha TEXT CHECK (checkpoint_sha IS NULL OR (length(checkpoint_sha)=64 AND checkpoint_sha NOT GLOB '*[^0-9a-f]*')),
        value REAL NOT NULL,
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(attempt_id, metric, partition),
        UNIQUE(variant_id, lock_id, metric, partition, seed));
CREATE TABLE gate_replicate_groups (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        metric TEXT NOT NULL, unit TEXT NOT NULL,
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL,
        membership_digest TEXT NOT NULL CHECK (length(membership_digest)=64 AND membership_digest NOT GLOB '*[^0-9a-f]*'),
        ci_method TEXT NOT NULL CHECK (ci_method IN ('t_interval_95','percentile_bootstrap_95')),
        statistics_version INTEGER NOT NULL CHECK (statistics_version=1),
        n INTEGER NOT NULL CHECK (n >= 2),
        mean REAL NOT NULL, std REAL NOT NULL, ci_low REAL NOT NULL, ci_high REAL NOT NULL,
        min_sample_count INTEGER NOT NULL CHECK (min_sample_count >= 1),
        sources_json TEXT NOT NULL, excluded_attempts_json TEXT NOT NULL,
        result_digest TEXT NOT NULL CHECK (length(result_digest)=64 AND result_digest NOT GLOB '*[^0-9a-f]*'),
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK (ci_low <= mean AND mean <= ci_high));
CREATE TABLE gate_replicate_members (
        group_id TEXT NOT NULL REFERENCES gate_replicate_groups(id),
        metric_record_id TEXT NOT NULL REFERENCES gate_metric_records(id),
        PRIMARY KEY(group_id, metric_record_id));
CREATE TABLE gate_research_program_outcomes (
        program_id TEXT NOT NULL REFERENCES gate_research_programs(id),
        outcome TEXT NOT NULL CHECK (length(outcome) BETWEEN 1 AND 64),
        study_id TEXT NOT NULL REFERENCES research_studies(id),
        title TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(program_id, outcome), UNIQUE(program_id, study_id));
CREATE TABLE gate_research_programs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, objective TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(project_id, name));
CREATE TABLE gate_split_lock_members (
        lock_id TEXT NOT NULL REFERENCES gate_split_locks(id),
        partition TEXT NOT NULL, member_key TEXT NOT NULL, group_key TEXT,
        PRIMARY KEY(lock_id, partition, member_key));
CREATE TABLE gate_split_locks (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, dataset_snapshot_id TEXT REFERENCES dataset_snapshots(id),
        membership_digest TEXT NOT NULL CHECK (length(membership_digest)=64 AND membership_digest NOT GLOB '*[^0-9a-f]*'),
        partitions_json TEXT NOT NULL,
        member_count INTEGER NOT NULL CHECK (member_count >= 1),
        created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(project_id, membership_digest));
CREATE TABLE generated_output_disclosures (
        id TEXT PRIMARY KEY, output_id TEXT NOT NULL REFERENCES generated_public_outputs(id),
        issued_by TEXT NOT NULL REFERENCES principals(id), bounds_json TEXT NOT NULL,
        revision INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE generated_output_grants (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        issued_by TEXT NOT NULL REFERENCES principals(id), bounds_json TEXT NOT NULL,
        revision INTEGER NOT NULL, revoked INTEGER NOT NULL DEFAULT 0,
        used_outputs INTEGER NOT NULL DEFAULT 0, used_bytes INTEGER NOT NULL DEFAULT 0);
CREATE TABLE generated_public_outputs (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        grant_id TEXT NOT NULL REFERENCES generated_output_grants(id),
        actor_id TEXT NOT NULL REFERENCES principals(id), credential_id TEXT NOT NULL,
        sha256 TEXT NOT NULL, size INTEGER NOT NULL, content BLOB NOT NULL,
        metadata_json TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(grant_id,sha256));
CREATE TABLE idempotency (
            principal_id TEXT NOT NULL REFERENCES principals(id),
            org_id TEXT NOT NULL REFERENCES organizations(id),
            idempotency_key TEXT NOT NULL,
            method TEXT NOT NULL,
            path TEXT NOT NULL,
            request_digest TEXT NOT NULL,
            response_status INTEGER NOT NULL,
            response_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (principal_id, org_id, idempotency_key, method, path)
        );
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','project','POST','/projects','60652c0e0772a97007a57a12a06057078851d2e28f781dbb27ba76149ed9978d',201,'{"created_at":"2026-11-20T19:17:29.389048Z","id":"project_ba8781fd818b4e20b2189879f3967aec","name":"Pilot","org_id":"org_local"}','2026-11-20T19:17:29.389162Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','snapshot','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/snapshots','1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500',201,'{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","created_at":"2026-11-20T19:17:29.404293Z","data_class":{"actor_principal_id":null,"data_class":"private_derived","decision_class":null,"license":null,"origin":"default","recorded_at":"2026-11-20T19:17:29.404293Z","source":null},"digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","id":"snapshot_f128bc3275b34924b75ec6951e10f5d1","manifest":{"entries":[{"classification":"unclassified","provenance":"imported_unverified","relative_path":"input.txt","sha256":"6c731e3de9a3b004da9ee6d1a04ebf16fbd11e2e945fd3c797d80bf0b07a6ec9","size":15}],"exclusions":[],"root":"/fixture/workspace","total_bytes":15},"project_id":"project_ba8781fd818b4e20b2189879f3967aec","transfer_authorized":false}','2026-11-20T19:17:29.404575Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','release','POST','/snapshots/snapshot_f128bc3275b34924b75ec6951e10f5d1/releases','4960a059a6cf35fdb28deaf207bc6fb9a31f4c2d8b291de383161620d71d81ce',201,'{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","classification":"public","created_at":"2026-11-20T19:17:29.411162Z","digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","expires_at":"2026-11-20T19:47:29.404889+00:00","id":"release_91eeb00752b747cd9ec3df33cf9da7a7","rationale":"Local CPU pilot input release.","revision":1,"revoked_at":null,"snapshot_id":"snapshot_f128bc3275b34924b75ec6951e10f5d1","status":"active","worker_principal_id":"worker-worker-token"}','2026-11-20T19:17:29.411291Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','task-First','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/tasks','0087a1de16e022bdaf82e481cc6b8489eec5934ee3a80b942675f8c7a53517bf',201,'{"budget_cents":100,"created_at":"2026-11-20T19:17:29.417085Z","current_plan_digest":"bd944782a62c84697e384d71611dd7693b23ea08f50a6465d0080a36c64e6da0","execution_granted":false,"id":"task_d63915ceaaac47c4a4b8f082be1b5bca","max_duration_seconds":60,"plan_digest":"bd944782a62c84697e384d71611dd7693b23ea08f50a6465d0080a36c64e6da0","project_id":"project_ba8781fd818b4e20b2189879f3967aec","purpose":"Exercise durable local execution authority.","revision":1,"status":"draft","title":"First","updated_at":"2026-11-20T19:17:29.417085Z"}','2026-11-20T19:17:29.417236Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','revise-First','PUT','/tasks/task_d63915ceaaac47c4a4b8f082be1b5bca/plan','c15375919a0ca18cb4e65bb0e8153189fe31de8ed57d59275d9e2b0b79b367f6',200,'{"budget_cents":100,"created_at":"2026-11-20T19:17:29.417085Z","current_plan_digest":"0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611","execution_granted":false,"id":"task_d63915ceaaac47c4a4b8f082be1b5bca","max_duration_seconds":60,"plan_digest":"0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611","project_id":"project_ba8781fd818b4e20b2189879f3967aec","purpose":"Exercise durable local execution authority.","revision":2,"status":"draft","title":"First","updated_at":"2026-11-20T19:17:29.419320Z"}','2026-11-20T19:17:29.419465Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','approve-First','POST','/tasks/task_d63915ceaaac47c4a4b8f082be1b5bca/approve','9369c41c48f76e59bc2e3d94f35a88a3c21d0fb40427f1f8e85f613449b1897d',200,'{"budget_cents":100,"created_at":"2026-11-20T19:17:29.417085Z","current_plan_digest":"0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611","execution_granted":false,"id":"task_d63915ceaaac47c4a4b8f082be1b5bca","max_duration_seconds":60,"plan_digest":"0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611","project_id":"project_ba8781fd818b4e20b2189879f3967aec","purpose":"Exercise durable local execution authority.","revision":3,"status":"approved","title":"First","updated_at":"2026-11-20T19:17:29.421527Z"}','2026-11-20T19:17:29.421645Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','limits','PUT','/projects/project_ba8781fd818b4e20b2189879f3967aec/execution-limits','4db99b2ab15e00afb0940c0d98155c4b9cbe60f3064a003353898dc7816caa88',201,'{"created_at":"2026-11-20T19:17:29.427462Z","currency":"USD","held_cents":0,"max_concurrency":1,"monetary_limit_cents":100,"operational_only":true,"project_id":"project_ba8781fd818b4e20b2189879f3967aec","revision":1,"settled_cents":0,"updated_at":"2026-11-20T19:17:29.427462Z"}','2026-11-20T19:17:29.427565Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','two-path-revise','PUT','/tasks/task_d63915ceaaac47c4a4b8f082be1b5bca/plan','f1a2c4c677292a1467ea67df020d722b7082c77dda329ea7b619dfd389a71af8',200,'{"budget_cents":100,"created_at":"2026-11-20T19:17:29.417085Z","current_plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","execution_granted":false,"id":"task_d63915ceaaac47c4a4b8f082be1b5bca","max_duration_seconds":60,"plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","project_id":"project_ba8781fd818b4e20b2189879f3967aec","purpose":"Exercise durable local execution authority.","revision":4,"status":"draft","title":"First","updated_at":"2026-11-20T19:17:29.436730Z"}','2026-11-20T19:17:29.436911Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','two-path-approve','POST','/tasks/task_d63915ceaaac47c4a4b8f082be1b5bca/approve','6ec0fd927720eb9be6eae9db64a3d50d3e364a358ded8599943af6b9c252b629',200,'{"budget_cents":100,"created_at":"2026-11-20T19:17:29.417085Z","current_plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","execution_granted":false,"id":"task_d63915ceaaac47c4a4b8f082be1b5bca","max_duration_seconds":60,"plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","project_id":"project_ba8781fd818b4e20b2189879f3967aec","purpose":"Exercise durable local execution authority.","revision":5,"status":"approved","title":"First","updated_at":"2026-11-20T19:17:29.442166Z"}','2026-11-20T19:17:29.442279Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','two-path-grant','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/execution-grants','2a4cd7177702aa699b411d41b08bf24ca483c14cbd5a440b584e2ea780932a45',201,'{"acknowledges_unmeasured_infrastructure":true,"cancellation":{"state":"not_requested"},"created_at":"2026-11-20T19:17:29.444600Z","created_by_principal_id":"principal_12227679343446a2959e088ba1daf770","currency":"USD","desired_state":"admitted","epoch":1,"execution_dispatched":false,"expires_at":"2026-11-20T19:37:29.442541Z","id":"grant_7787a24afa2f4948a6428db2dc0e110e","max_exposure_cents":60,"operational_only":true,"pinned_image":"python@sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","project_id":"project_ba8781fd818b4e20b2189879f3967aec","recovery_reconciliation":null,"revision":1,"snapshot_digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","snapshot_id":"snapshot_f128bc3275b34924b75ec6951e10f5d1","snapshot_release_expires_at":"2026-11-20T19:47:29.404889Z","snapshot_release_id":"release_91eeb00752b747cd9ec3df33cf9da7a7","status":"active","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca","task_revision":5,"updated_at":"2026-11-20T19:17:29.444600Z","worker_principal_id":"worker-worker-token"}','2026-11-20T19:17:29.444860Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','two-path-lease','POST','/execution-grants/grant_7787a24afa2f4948a6428db2dc0e110e/lease','lease',201,'{"epoch":2,"execution_dispatched":false,"expires_at":"2026-11-20T19:17:59.447935Z","grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","hard_expires_at":"2026-11-20T19:18:29.447935Z","id":"lease_71b2e54c24d040ebb9e0494eb6891fee","issued_at":"2026-11-20T19:17:29.447935Z","operational_only":true,"renewed_at":"2026-11-20T19:17:29.447935Z","worker_principal_id":"worker-worker-token"}','2026-11-20T19:17:29.448096Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','first','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/artifacts','583692501e183973408ba2384d493eafcd006cafaed6189590dc588b25dc4460',201,'{"classification":"unclassified","created_at":"2026-11-20T19:17:29.454223Z","epoch":2,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","id":"artifact_80bbef8fd2992c60467394b2bebaae1b","late":false,"lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","operation_id":"94cb5622d57c29865de7b74891860b47","project_id":"project_ba8781fd818b4e20b2189879f3967aec","relative_path":"first.txt","sha256":"d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579","size":6,"status":"quarantined","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca","worker_principal_id":"worker-worker-token"}','2026-11-20T19:17:29.454372Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','begin','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/artifact-uploads','058e4f91ee0330d3c454b7152ffa63399f43ac4bce02d751373268781128f002',201,'{"id":"artifact_upload_cb220f89361ab592d4f3a7590b05a1ec","lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","received_bytes":0,"relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"status":"uploading"}','2026-11-20T19:17:29.456653Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','chunk','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/artifact-uploads/artifact_upload_cb220f89361ab592d4f3a7590b05a1ec/chunks','2052fc2d2c8b99d03d40eba995ee030eeb85fe8febf7faaa1ac537c1974628d1',200,'{"id":"artifact_upload_cb220f89361ab592d4f3a7590b05a1ec","lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","received_bytes":7,"relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"status":"uploading"}','2026-11-20T19:17:29.458809Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','complete','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/artifact-uploads/artifact_upload_cb220f89361ab592d4f3a7590b05a1ec/complete','44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a',201,'{"classification":"unclassified","created_at":"2026-11-20T19:17:29.460997Z","epoch":2,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","id":"artifact_cb220f89361ab592d4f3a7590b05a1ec","late":false,"lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","operation_id":"94cb5622d57c29865de7b74891860b47","project_id":"project_ba8781fd818b4e20b2189879f3967aec","relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"status":"quarantined","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca","worker_principal_id":"worker-worker-token"}','2026-11-20T19:17:29.461107Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','placement-intent:place','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/project-artifacts/intents','41e7401361fd1533954bbd93af5a2b604d5797c88c75b6b55eb07d1f33f8ed56',202,'{"operations":[{"artifact_id":"artifact_80bbef8fd2992c60467394b2bebaae1b","operation_key":"project-artifact-1a36e01fb2f0e311666b31ac821dc29b3c1fe1b42cba7363c2c80412625e9f17","retention_class":"project_default","sha256":"d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579","size":6},{"artifact_id":"artifact_cb220f89361ab592d4f3a7590b05a1ec","operation_key":"project-artifact-6f96cfc628df3a21c1697464db48b5c8c2253192a890e4a2322c93b7e0aea7c3","retention_class":"project_default","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7}],"project_id":"project_ba8781fd818b4e20b2189879f3967aec","retention_class":"project_default","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca"}','2026-11-20T19:17:29.472360Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','placement-reconcile:place','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/project-artifacts/reconcile','41e7401361fd1533954bbd93af5a2b604d5797c88c75b6b55eb07d1f33f8ed56',200,'{"state":"reconciling"}','2026-11-20T19:17:29.474432Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','place','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/project-artifacts','41e7401361fd1533954bbd93af5a2b604d5797c88c75b6b55eb07d1f33f8ed56',201,'{"placements":[{"artifact_id":"artifact_80bbef8fd2992c60467394b2bebaae1b","committed_at":"2026-11-20T19:17:29.478873Z","retention_class":"project_default","sha256":"d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579","size":6,"state":"committed"},{"artifact_id":"artifact_cb220f89361ab592d4f3a7590b05a1ec","committed_at":"2026-11-20T19:17:29.478873Z","retention_class":"project_default","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7,"state":"committed"}],"storage_backend":"filesystem-local-v1"}','2026-11-20T19:17:29.479086Z');
INSERT INTO "idempotency" VALUES('worker-worker-token','org_local','manifest','POST','/worker/leases/lease_71b2e54c24d040ebb9e0494eb6891fee/artifact-manifest','f29516295697f70c26a2115a7cf75ee816b1248cbe996d064a0a5baa7d7e34f9',201,'{"artifacts":[{"id":"artifact_80bbef8fd2992c60467394b2bebaae1b","relative_path":"first.txt","sha256":"d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579","size":6},{"id":"artifact_cb220f89361ab592d4f3a7590b05a1ec","relative_path":"second.txt","sha256":"59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395","size":7}],"classification":"unclassified","created_at":"2026-11-20T19:17:29.486940Z","epoch":2,"grant_id":"grant_7787a24afa2f4948a6428db2dc0e110e","id":"artifact_manifest_441fb0c2f575ef75910675af3654b775","late":false,"lease_id":"lease_71b2e54c24d040ebb9e0494eb6891fee","manifest_digest":"43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc","operation_id":"94cb5622d57c29865de7b74891860b47","plan_digest":"9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7","project_id":"project_ba8781fd818b4e20b2189879f3967aec","status":"quarantined","task_id":"task_d63915ceaaac47c4a4b8f082be1b5bca","total_bytes":13,"worker_principal_id":"worker-worker-token"}','2026-11-20T19:17:29.487081Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','retain','POST','/artifact-manifests/artifact_manifest_441fb0c2f575ef75910675af3654b775/dispositions','885402c08c71bd154adabe19d30caebd2a2fb24b8a46293cfa47c15acc1b91fb',201,'{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","created_at":"2026-11-20T19:17:29.492983Z","disposition":"retain_quarantined","id":"artifact_disposition_artifact_manifest_441fb0c2f575ef75910675af3654b775_1","manifest_digest":"43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc","manifest_id":"artifact_manifest_441fb0c2f575ef75910675af3654b775","rationale":"retain bounded output under quarantine","revision":1}','2026-11-20T19:17:29.493115Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','release','POST','/artifact-manifests/artifact_manifest_441fb0c2f575ef75910675af3654b775/custody-releases','6dded9df516c3720b7e83a778653f60b890d2eeff67e68ae32ce9c69c95d2eee',201,'{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","created_at":"2026-11-20T19:17:29.498600Z","id":"artifact_custody_release_artifact_manifest_441fb0c2f575ef75910675af3654b775_1","manifest_digest":"43ebb2c68ccab710c8a5fb0444ff64013b226acf093547d9eff4537311f0d7dc","manifest_id":"artifact_manifest_441fb0c2f575ef75910675af3654b775","rationale":"researcher may inspect private retained output","revision":1,"state":"released"}','2026-11-20T19:17:29.498705Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','pre1-begin','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/chunked-snapshots','d1961d153d57258ab689b43742bc5ac11afc055419ad96025eb3ec5f50b2f6a6',201,'{"chunk_bytes":4194304,"chunk_plan":[{"offset":0,"relative_path":"m.py","seq":0,"size":6}],"expected_digest":"234602367fcc57299510b5f8340b9dc86f0a1d1876650b02ae86f35b96b1c9f4","project_id":"project_ba8781fd818b4e20b2189879f3967aec","total_bytes":6,"upload_id":"snapshot_aa8f35d72c4948b3866b162b45f07574"}','2026-11-20T19:17:29.505107Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','pre1-done','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/chunked-snapshots/snapshot_aa8f35d72c4948b3866b162b45f07574/complete','0595c9c30ea0b89fff6ed9a84737f91228874f4a2745d7a311750e37bf5ac136',201,'{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","chunk_count":1,"chunked":true,"created_at":"2026-11-20T19:17:29.509169Z","data_class":{"actor_principal_id":null,"data_class":"private_derived","decision_class":null,"license":null,"origin":"default","recorded_at":"2026-11-20T19:17:29.509169Z","source":null},"digest":"234602367fcc57299510b5f8340b9dc86f0a1d1876650b02ae86f35b96b1c9f4","file_count":1,"id":"snapshot_aa8f35d72c4948b3866b162b45f07574","project_id":"project_ba8781fd818b4e20b2189879f3967aec","total_bytes":6,"transfer_authorized":false}','2026-11-20T19:17:29.509386Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','pre2-begin','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/chunked-snapshots','a2396c65b8050a65d0bb0dde9ab244ca3a3d030b1c205786ce6d775e65c319df',201,'{"chunk_bytes":4194304,"chunk_plan":[{"offset":0,"relative_path":"rows.csv","seq":0,"size":2}],"expected_digest":"15618551f69b87c9ee4f4ddd5a9f9f5ece77aec6dc5c114721520a64710bfb70","project_id":"project_ba8781fd818b4e20b2189879f3967aec","total_bytes":2,"upload_id":"snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8"}','2026-11-20T19:17:29.511684Z');
INSERT INTO "idempotency" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','pre2-done','POST','/projects/project_ba8781fd818b4e20b2189879f3967aec/chunked-snapshots/snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8/complete','a6177a02d8f4da535161ed5f7b412bcfe5043634b936179fa541bbc0f03885f4',201,'{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","chunk_count":1,"chunked":true,"created_at":"2026-11-20T19:17:29.515922Z","data_class":{"actor_principal_id":"principal_12227679343446a2959e088ba1daf770","data_class":"public","decision_class":"owner_automation_labelled","license":"CC0-1.0","origin":"human_declared","recorded_at":"2026-11-20T19:17:29.511507Z","source":"https://example.org/data"},"digest":"15618551f69b87c9ee4f4ddd5a9f9f5ece77aec6dc5c114721520a64710bfb70","file_count":1,"id":"snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8","project_id":"project_ba8781fd818b4e20b2189879f3967aec","total_bytes":2,"transfer_authorized":false}','2026-11-20T19:17:29.516170Z');
CREATE TABLE interaction_message_deliveries (
            id TEXT PRIMARY KEY, message_id TEXT NOT NULL UNIQUE REFERENCES interaction_messages(id),
            org_id TEXT NOT NULL REFERENCES organizations(id), project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT NOT NULL REFERENCES tasks(id),
            operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
            grant_id TEXT NOT NULL REFERENCES execution_grants(id),
            lease_id TEXT NOT NULL REFERENCES execution_leases(id),
            harness_session_binding_id TEXT REFERENCES operation_harness_session_bindings(id),
            admitted_attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
            attempt_revision INTEGER NOT NULL, context_revision INTEGER NOT NULL,
            epoch INTEGER NOT NULL, worker_principal_id TEXT NOT NULL REFERENCES principals(id),
            harness_instance_id TEXT NOT NULL, upstream_session_id TEXT NOT NULL,
            message_digest TEXT NOT NULL, context_digest TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('queued','delivered','acknowledged','uncertain','failed','cancelled')),
            claimed_by_principal_id TEXT REFERENCES principals(id), upstream_message_id TEXT,
            upstream_readback_digest TEXT, error_code TEXT, revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
CREATE TABLE interaction_message_delivery_states (
            delivery_id TEXT NOT NULL REFERENCES interaction_message_deliveries(id),
            sequence INTEGER NOT NULL, state TEXT NOT NULL,
            context_digest TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
            error_code TEXT, recorded_at TEXT NOT NULL,
            PRIMARY KEY (delivery_id, sequence)
        );
CREATE TABLE interaction_messages (
            id TEXT PRIMARY KEY, thread_id TEXT NOT NULL REFERENCES interaction_threads(id),
            sender_principal_id TEXT NOT NULL REFERENCES principals(id), intent TEXT NOT NULL
                CHECK (intent IN ('question','direction','status','evidence')),
            body TEXT NOT NULL CHECK (length(body) <= 8000),
            reply_to TEXT REFERENCES interaction_messages(id),
            created_at TEXT NOT NULL
        );
CREATE TABLE interaction_threads (
            id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id), task_id TEXT REFERENCES tasks(id),
            title TEXT NOT NULL, created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id)
        );
CREATE TABLE kubernetes_dispatches (
        id TEXT PRIMARY KEY,
        operation_id TEXT NOT NULL UNIQUE REFERENCES external_operations(operation_id),
        admitted_attempt_id TEXT NOT NULL UNIQUE
            REFERENCES research_experiment_admitted_attempts(id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        package_digest TEXT NOT NULL CHECK (
            length(package_digest)=64 AND package_digest NOT GLOB '*[^0-9a-f]*'),
        target_digest TEXT NOT NULL CHECK (
            length(target_digest)=64 AND target_digest NOT GLOB '*[^0-9a-f]*'),
        profile_version TEXT NOT NULL,
        state TEXT NOT NULL CHECK (state IN ('queued','claimed','cancel_requested')),
        claimed_at TEXT, claimed_by_principal_id TEXT REFERENCES principals(id),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id));
CREATE TABLE operation_harness_session_bindings (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            project_id TEXT NOT NULL REFERENCES projects(id),
            task_id TEXT NOT NULL REFERENCES tasks(id),
            operation_id TEXT NOT NULL UNIQUE REFERENCES external_operations(operation_id),
            admitted_attempt_id TEXT NOT NULL UNIQUE REFERENCES research_experiment_admitted_attempts(id),
            attempt_revision INTEGER NOT NULL CHECK (attempt_revision >= 1),
            grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
            lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
            worker_principal_id TEXT NOT NULL REFERENCES principals(id),
            epoch INTEGER NOT NULL CHECK (epoch >= 1),
            harness_instance_id TEXT NOT NULL,
            upstream_session_id TEXT NOT NULL,
            adapter_profile TEXT NOT NULL CHECK (adapter_profile = 'opencode-v1.18.30-context-only'),
            provider_disabled INTEGER NOT NULL CHECK (provider_disabled = 1),
            created_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            UNIQUE(worker_principal_id, harness_instance_id, upstream_session_id)
        );
CREATE TABLE operator_audit_events (
        cursor INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT REFERENCES projects(id),
        event_type TEXT NOT NULL,
        decision TEXT NOT NULL CHECK (decision IN ('allowed','denied')),
        actor_principal_id TEXT REFERENCES principals(id),
        correlation_id TEXT,
        causation_id TEXT,
        created_at TEXT NOT NULL,
        payload_json TEXT NOT NULL
        );
CREATE TABLE operator_backup_health (
        singleton INTEGER PRIMARY KEY CHECK (singleton=1),
        last_success_at TEXT,
        last_backup_id TEXT,
        last_failure_at TEXT,
        last_failure_code TEXT
        );
INSERT INTO "operator_backup_health" VALUES(1,NULL,NULL,NULL,NULL);
CREATE TABLE ordinary_docker_dispatches (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        target_id TEXT NOT NULL REFERENCES ordinary_docker_targets(id),
        attempt_id TEXT NOT NULL UNIQUE REFERENCES research_experiment_admitted_attempts(id),
        package_json TEXT NOT NULL, package_digest TEXT NOT NULL,
        state TEXT NOT NULL, container_id TEXT, claimed_by TEXT REFERENCES principals(id),
        report_json TEXT, revision INTEGER NOT NULL);
CREATE TABLE ordinary_docker_targets (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        issued_by TEXT NOT NULL REFERENCES principals(id), bounds_json TEXT NOT NULL,
        target_digest TEXT NOT NULL, revision INTEGER NOT NULL,
        revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE organizations (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
INSERT INTO "organizations" VALUES('org_local','Local personal organization','2026-11-20T19:17:29.375464Z');
CREATE TABLE principals (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            role TEXT NOT NULL CHECK (role IN ('human', 'agent')),
            token_hash TEXT NOT NULL UNIQUE,
            revoked_at TEXT,
            created_at TEXT NOT NULL
        );
INSERT INTO "principals" VALUES('principal_12227679343446a2959e088ba1daf770','org_local','human','981d60e69aa038c7745f6789d2b6b1609232498449c667e32da96250511c19cc',NULL,'2026-11-20T19:17:29.375464Z');
INSERT INTO "principals" VALUES('worker-worker-token','org_local','agent','dedd02022dba76562722a9463558e4124c82c5e6a09e925e6b11eec9a7042bf7',NULL,'2026-11-20T19:17:29.389435Z');
CREATE TABLE project_artifact_placements (
        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        tenant_namespace TEXT NOT NULL,
        project_namespace TEXT NOT NULL,
        object_key TEXT NOT NULL,
        sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        committed_at TEXT NOT NULL,
        reconciled_at TEXT NOT NULL,
        UNIQUE(project_id, backend_kind, object_key)
        );
INSERT INTO "project_artifact_placements" VALUES('artifact_80bbef8fd2992c60467394b2bebaae1b','project_ba8781fd818b4e20b2189879f3967aec','filesystem-local-v1','dfd260aeb392993915962cceefe3235cc5709d40362e0ce4476a2cec282832ac','4310b1904e2756584652aad437b46161883b7a8360218f0c618ac300bc1674e9','4a61c03f24e8b2fdb0d6cf5233dab1ece9f9d248b3bace43eaea61b28a34d285','d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579',6,'project_default','2026-11-20T19:17:29.478873Z','2026-11-20T19:17:29.478873Z');
INSERT INTO "project_artifact_placements" VALUES('artifact_cb220f89361ab592d4f3a7590b05a1ec','project_ba8781fd818b4e20b2189879f3967aec','filesystem-local-v1','dfd260aeb392993915962cceefe3235cc5709d40362e0ce4476a2cec282832ac','4310b1904e2756584652aad437b46161883b7a8360218f0c618ac300bc1674e9','0968b76a9b1a978195abf52e1ecc8c1526e941e982cd0fe08d9c8ab9065b8ad8','59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395',7,'project_default','2026-11-20T19:17:29.478873Z','2026-11-20T19:17:29.478873Z');
CREATE TABLE project_artifact_retention (
        artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held', 'short_lived')),
        state TEXT NOT NULL CHECK (state IN ('active', 'tombstoned', 'delete_intent', 'degraded', 'deleted')),
        created_at TEXT NOT NULL, activity_at TEXT NOT NULL, tombstoned_at TEXT,
        delete_intent_at TEXT, deleted_at TEXT, incident_at TEXT, incident_reason TEXT
        );
INSERT INTO "project_artifact_retention" VALUES('artifact_80bbef8fd2992c60467394b2bebaae1b','project_ba8781fd818b4e20b2189879f3967aec','project_default','active','2026-11-20T19:17:29.478873Z','2026-11-20T19:17:29.478873Z',NULL,NULL,NULL,NULL,NULL);
INSERT INTO "project_artifact_retention" VALUES('artifact_cb220f89361ab592d4f3a7590b05a1ec','project_ba8781fd818b4e20b2189879f3967aec','project_default','active','2026-11-20T19:17:29.478873Z','2026-11-20T19:17:29.478873Z',NULL,NULL,NULL,NULL,NULL);
CREATE TABLE project_artifact_storage_operations (
        id TEXT PRIMARY KEY,
        artifact_id TEXT UNIQUE REFERENCES artifacts(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        operation_key TEXT NOT NULL UNIQUE,
        state TEXT NOT NULL CHECK (state IN ('intent', 'reconciling', 'committed')),
        sha256 TEXT NOT NULL CHECK (length(sha256)=64 AND sha256 NOT GLOB '*[^0-9a-f]*'),
        size INTEGER NOT NULL CHECK (size >= 0),
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        intent_at TEXT NOT NULL,
        reconcile_started_at TEXT,
        committed_at TEXT,
        reconciled_at TEXT,
        last_error TEXT
        );
INSERT INTO "project_artifact_storage_operations" VALUES('project_storage_operation_a61ce06692afdf7a6e4c2ee124844005','artifact_80bbef8fd2992c60467394b2bebaae1b','project_ba8781fd818b4e20b2189879f3967aec','filesystem-local-v1','project-artifact-1a36e01fb2f0e311666b31ac821dc29b3c1fe1b42cba7363c2c80412625e9f17','committed','d15690f08a575024650b01ffac892cfd2b93e6c57c140f1b6d9e47753cabd579',6,'project_default','2026-11-20T19:17:29.472208Z','2026-11-20T19:17:29.474371Z','2026-11-20T19:17:29.478873Z','2026-11-20T19:17:29.478873Z',NULL);
INSERT INTO "project_artifact_storage_operations" VALUES('project_storage_operation_53ce335f2b3c2b382f4e41e910a71479','artifact_cb220f89361ab592d4f3a7590b05a1ec','project_ba8781fd818b4e20b2189879f3967aec','filesystem-local-v1','project-artifact-6f96cfc628df3a21c1697464db48b5c8c2253192a890e4a2322c93b7e0aea7c3','committed','59fdd34a0b323f6044e147b843d8ca246a9414d2c7a06c8e95a7e04c3cdaf395',7,'project_default','2026-11-20T19:17:29.472208Z','2026-11-20T19:17:29.474371Z','2026-11-20T19:17:29.478873Z','2026-11-20T19:17:29.478873Z',NULL);
CREATE TABLE project_storage_bindings (
        project_id TEXT PRIMARY KEY REFERENCES projects(id),
        backend_kind TEXT NOT NULL,
        retention_class TEXT NOT NULL CHECK (retention_class IN ('project_default', 'held')),
        bound_at TEXT NOT NULL
        );
INSERT INTO "project_storage_bindings" VALUES('project_ba8781fd818b4e20b2189879f3967aec','filesystem-local-v1','project_default','2026-11-20T19:17:29.472208Z');
CREATE TABLE projects (
            id TEXT PRIMARY KEY,
            org_id TEXT NOT NULL REFERENCES organizations(id),
            name TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
INSERT INTO "projects" VALUES('project_ba8781fd818b4e20b2189879f3967aec','org_local','Pilot','2026-11-20T19:17:29.389048Z');
CREATE TABLE provider_input_tickets (
        token_hash TEXT PRIMARY KEY CHECK(length(token_hash)=64),
        operation_id TEXT NOT NULL REFERENCES external_operations(operation_id),
        snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        snapshot_digest TEXT NOT NULL, lease_id TEXT NOT NULL, epoch INTEGER NOT NULL,
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        expires_at TEXT NOT NULL, revoked_at TEXT,
        chunks_served INTEGER NOT NULL DEFAULT 0, last_served_at TEXT,
        created_at TEXT NOT NULL
        );
CREATE TABLE remote_workflow_checkpoints (
        operation_id TEXT NOT NULL REFERENCES remote_workflow_runs(operation_id),
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        checkpoint_cursor TEXT NOT NULL,
        checkpoint_digest TEXT NOT NULL CHECK (
            length(checkpoint_digest)=64 AND checkpoint_digest NOT GLOB '*[^0-9a-f]*'),
        recorded_at TEXT NOT NULL,
        PRIMARY KEY(operation_id, sequence), UNIQUE(operation_id, checkpoint_cursor));
CREATE TABLE remote_workflow_runs (
        operation_id TEXT PRIMARY KEY REFERENCES external_operations(operation_id),
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        lease_id TEXT NOT NULL UNIQUE REFERENCES execution_leases(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        epoch INTEGER NOT NULL CHECK (epoch >= 1),
        experiment_attempt_id TEXT,
        experiment_id TEXT,
        experiment_design_revision INTEGER,
        experiment_variant_id TEXT,
        state TEXT NOT NULL CHECK (state IN ('prepared','running','unknown','exited')),
        last_heartbeat_at TEXT,
        last_checkpoint_sequence INTEGER NOT NULL DEFAULT 0 CHECK (last_checkpoint_sequence >= 0),
        last_checkpoint_cursor TEXT,
        last_checkpoint_digest TEXT,
        last_reconnected_at TEXT,
        control_outage_at TEXT,
        last_observed_at TEXT,
        last_observed_state TEXT CHECK (last_observed_state IN ('running','unknown','exited')),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE reproduction_bundles (
        id TEXT PRIMARY KEY,
        manifest_id TEXT NOT NULL REFERENCES artifact_manifests(id),
        manifest_digest TEXT NOT NULL CHECK (length(manifest_digest)=64
            AND manifest_digest NOT GLOB '*[^0-9a-f]*'),
        custody_release_id TEXT NOT NULL REFERENCES artifact_custody_releases(id),
        bundle_digest TEXT NOT NULL UNIQUE CHECK (length(bundle_digest)=64
            AND bundle_digest NOT GLOB '*[^0-9a-f]*'),
        payload_json TEXT NOT NULL CHECK (length(payload_json) <= 4194304),
        size INTEGER NOT NULL CHECK (size >= 1 AND size <= 4194304),
        created_at TEXT NOT NULL,
        actor_principal_id TEXT NOT NULL REFERENCES principals(id)
        );
CREATE TABLE research_admitted_attempt_artifacts (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id), created_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,artifact_id));
CREATE TABLE research_admitted_attempt_measurements (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL
        REFERENCES research_experiment_admitted_attempts(id), name TEXT NOT NULL,
        value_json TEXT NOT NULL, artifact_id TEXT REFERENCES artifacts(id),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id), source TEXT NOT NULL DEFAULT 'human_entered' CHECK (source IN ('human_entered','derived')));
CREATE TABLE research_admitted_conclusion_evidence (
        conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        artifact_id TEXT REFERENCES artifacts(id), measurement_id TEXT,
        PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
        CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
               (artifact_id IS NULL AND measurement_id IS NOT NULL)));
CREATE TABLE research_attempt_artifacts (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT NOT NULL REFERENCES artifacts(id), created_at TEXT NOT NULL,
        PRIMARY KEY(attempt_id,artifact_id));
CREATE TABLE research_attempt_measurements (
        id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        name TEXT NOT NULL, value_json TEXT NOT NULL, artifact_id TEXT REFERENCES artifacts(id),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id), source TEXT NOT NULL DEFAULT 'human_entered' CHECK (source IN ('human_entered','derived')));
CREATE TABLE research_conclusion_amendments (
        id TEXT PRIMARY KEY, conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        relation TEXT NOT NULL CHECK (relation IN ('supersedes','retracts')),
        reason TEXT NOT NULL, expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
        expected_design_revision INTEGER NOT NULL CHECK (expected_design_revision >= 1),
        prior_lineage_revision INTEGER NOT NULL CHECK (prior_lineage_revision >= 1),
        lineage_revision INTEGER NOT NULL CHECK (lineage_revision >= 1),
        created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(conclusion_id,lineage_revision));
CREATE TABLE research_conclusion_evidence (
        conclusion_id TEXT NOT NULL REFERENCES research_conclusions(id),
        attempt_id TEXT NOT NULL REFERENCES research_experiment_attempts(id),
        artifact_id TEXT REFERENCES artifacts(id), measurement_id TEXT REFERENCES research_attempt_measurements(id),
        PRIMARY KEY(conclusion_id,attempt_id,artifact_id,measurement_id),
        CHECK ((artifact_id IS NOT NULL AND measurement_id IS NULL) OR
               (artifact_id IS NULL AND measurement_id IS NOT NULL)));
CREATE TABLE research_conclusion_lineage (
        conclusion_id TEXT PRIMARY KEY REFERENCES research_conclusions(id),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        amendment_id TEXT UNIQUE, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL);
CREATE TABLE research_conclusions (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER,
        statement TEXT NOT NULL, status TEXT NOT NULL CHECK (status IN ('draft','reviewed')),
        revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id), reviewed_at TEXT,
        reviewed_by_principal_id TEXT REFERENCES principals(id),
        FOREIGN KEY(experiment_id,design_revision)
        REFERENCES research_experiment_designs(experiment_id,revision));
CREATE TABLE research_corrections (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            original_source_id TEXT NOT NULL REFERENCES research_sources(id),
            replacement_source_id TEXT NOT NULL REFERENCES research_sources(id),
            reason TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
            created_at TEXT NOT NULL);
CREATE TABLE research_experiment_admitted_attempt_resolutions (
        attempt_id TEXT PRIMARY KEY REFERENCES research_experiment_admitted_attempts(id),
        outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','cancelled','unknown')),
        reason TEXT, expected_revision INTEGER NOT NULL CHECK (expected_revision >= 1),
        decided_at TEXT NOT NULL, decided_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((outcome='succeeded') OR reason IS NOT NULL));
CREATE TABLE research_experiment_admitted_attempt_states (
        attempt_id TEXT NOT NULL REFERENCES research_experiment_admitted_attempts(id),
        sequence INTEGER NOT NULL CHECK (sequence >= 1),
        state TEXT NOT NULL CHECK (state IN ('admitted','preparing','running','unknown','interrupted','restart_pending','succeeded','failed','cancelled')),
        observed_state TEXT CHECK (observed_state IN ('unknown','running','exited')),
        reason TEXT, operation_id TEXT REFERENCES external_operations(operation_id),
        recorded_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY(attempt_id,sequence));
CREATE TABLE research_experiment_admitted_attempts (
        id TEXT PRIMARY KEY,
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER NOT NULL CHECK (design_revision >= 1),
        variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        plan_digest TEXT NOT NULL CHECK (length(plan_digest)=64 AND plan_digest NOT GLOB '*[^0-9a-f]*'),
        grant_id TEXT NOT NULL UNIQUE REFERENCES execution_grants(id),
        operation_id TEXT UNIQUE REFERENCES external_operations(operation_id),
        package_digest TEXT CHECK (package_digest IS NULL OR (length(package_digest)=64 AND package_digest NOT GLOB '*[^0-9a-f]*')),
        state TEXT NOT NULL CHECK (state IN ('admitted','preparing','running','unknown','interrupted','restart_pending','succeeded','failed','cancelled')),
        outcome TEXT CHECK (outcome IN ('succeeded','failed','cancelled','unknown')),
        reason TEXT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        resolved_by_principal_id TEXT REFERENCES principals(id), operational_state TEXT NOT NULL DEFAULT 'legacy_unverified' CHECK (operational_state IN ('admitted','preparing','running','unknown','interrupted','restart_pending','legacy_unverified')), operational_observed_state TEXT NOT NULL DEFAULT 'legacy_unverified' CHECK (operational_observed_state IN ('not_observed','running','unknown','exited','legacy_unverified')), operational_termination_confirmed INTEGER CHECK (operational_termination_confirmed IN (0,1)), operational_observation_cursor TEXT, operational_observed_at TEXT,
        CHECK ((outcome IS NULL AND reason IS NULL AND resolved_by_principal_id IS NULL)
               OR (outcome IS NOT NULL AND state=outcome AND resolved_by_principal_id IS NOT NULL)),
        FOREIGN KEY(experiment_id,design_revision)
        REFERENCES research_experiment_designs(experiment_id,revision)
        );
CREATE TABLE research_experiment_attempts (
        id TEXT PRIMARY KEY, variant_id TEXT NOT NULL REFERENCES research_experiment_variants(id),
        design_revision INTEGER NOT NULL CHECK (design_revision >= 1),
        provenance_kind TEXT NOT NULL CHECK (provenance_kind IN
            ('admitted_operation','human_reported_observation','legacy_operation_unverified')),
        operation_id TEXT REFERENCES external_operations(operation_id),
        execution_plan_digest TEXT, package_digest TEXT,
        operation_desired_state TEXT CHECK (operation_desired_state IN ('admitted','cancel_requested')),
        operation_observed_state TEXT NOT NULL CHECK (operation_observed_state IN
            ('exited','not_observed','legacy_unverified')),
        termination_status TEXT NOT NULL CHECK (termination_status IN
            ('confirmed','observed_exited_unconfirmed','not_applicable','legacy_unknown')),
        human_observation TEXT,
        outcome TEXT NOT NULL CHECK (outcome IN ('succeeded','failed','cancelled','inconclusive')),
        reason TEXT, revision INTEGER NOT NULL CHECK (revision >= 1), created_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        CHECK ((outcome = 'succeeded') OR reason IS NOT NULL),
        CHECK ((provenance_kind='admitted_operation' AND operation_id IS NOT NULL
               AND execution_plan_digest IS NOT NULL AND package_digest IS NOT NULL
               AND operation_desired_state IS NOT NULL AND operation_observed_state='exited'
               AND termination_status IN ('confirmed','observed_exited_unconfirmed')
               AND human_observation IS NULL)
            OR (provenance_kind='human_reported_observation' AND operation_id IS NULL
                AND execution_plan_digest IS NULL AND package_digest IS NULL
                AND operation_desired_state IS NULL AND operation_observed_state='not_observed'
                AND termination_status='not_applicable' AND human_observation IS NOT NULL)
            OR (provenance_kind='legacy_operation_unverified' AND operation_id IS NOT NULL
                AND operation_observed_state='legacy_unverified'
                AND termination_status='legacy_unknown')));
CREATE TABLE research_experiment_designs (
        experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        revision INTEGER NOT NULL CHECK (revision >= 1), goal TEXT NOT NULL, success_criterion TEXT NOT NULL,
        protocol TEXT NOT NULL,
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        PRIMARY KEY (experiment_id,revision));
CREATE TABLE research_experiment_variants (
        id TEXT PRIMARY KEY, experiment_id TEXT NOT NULL REFERENCES research_experiments(id),
        design_revision INTEGER NOT NULL, kind TEXT NOT NULL CHECK (kind IN ('baseline','intervention')),
        name TEXT NOT NULL, method_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
        metadata_json TEXT NOT NULL, execution_task_id TEXT REFERENCES tasks(id),
        execution_plan_digest TEXT, revision INTEGER NOT NULL CHECK (revision >= 1),
        created_at TEXT NOT NULL, created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(experiment_id,design_revision,kind),
        CHECK ((execution_task_id IS NULL) = (execution_plan_digest IS NULL)),
        FOREIGN KEY(experiment_id,design_revision) REFERENCES research_experiment_designs(experiment_id,revision));
CREATE TABLE research_experiments (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        goal TEXT NOT NULL, success_criterion TEXT NOT NULL,
        current_design_revision INTEGER NOT NULL CHECK (current_design_revision >= 1),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        created_by_principal_id TEXT NOT NULL REFERENCES principals(id));
CREATE TABLE research_finding_sources (
            finding_id TEXT NOT NULL REFERENCES research_findings(id),
            source_id TEXT NOT NULL REFERENCES research_sources(id),
            PRIMARY KEY(finding_id, source_id));
CREATE TABLE research_finding_variants (
            finding_id TEXT NOT NULL REFERENCES research_findings(id),
            variant_id TEXT NOT NULL REFERENCES research_variants(id),
            PRIMARY KEY(finding_id, variant_id));
CREATE TABLE research_findings (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            statement TEXT NOT NULL, status TEXT NOT NULL CHECK
            (status IN ('candidate','accepted','needs_review','retracted','superseded')),
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            accepted_by_principal_id TEXT REFERENCES principals(id),
            supersedes_finding_id TEXT UNIQUE REFERENCES research_findings(id),
            revision_kind TEXT CHECK (revision_kind IN ('narrow','supersede')),
            revision_reason TEXT,
            CHECK ((supersedes_finding_id IS NULL AND revision_kind IS NULL
                    AND revision_reason IS NULL)
                OR (supersedes_finding_id IS NOT NULL AND revision_kind IS NOT NULL
                    AND revision_reason IS NOT NULL)),
            CHECK (supersedes_finding_id IS NULL OR supersedes_finding_id != id));
CREATE TABLE research_sources (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            uri TEXT NOT NULL, quoted_passage TEXT NOT NULL, content_digest TEXT NOT NULL,
            locator TEXT, verdict TEXT NOT NULL CHECK
            (verdict IN ('unverified','supported','unsupported')),
            assessment_reason TEXT, revision INTEGER NOT NULL,
            supersedes_source_id TEXT REFERENCES research_sources(id),
            superseded_at TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            assessed_by_principal_id TEXT REFERENCES principals(id));
CREATE TABLE research_studies (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            purpose TEXT NOT NULL, protocol TEXT NOT NULL,
            mode TEXT NOT NULL CHECK (mode IN ('exploratory','confirmatory')),
            status TEXT NOT NULL CHECK (status IN ('open','closed')), revision INTEGER NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id));
CREATE TABLE research_variants (
            id TEXT PRIMARY KEY, study_id TEXT NOT NULL REFERENCES research_studies(id),
            name TEXT NOT NULL, method_digest TEXT NOT NULL, input_digest TEXT NOT NULL,
            metadata_json TEXT NOT NULL, replication_label TEXT NOT NULL CHECK
            (replication_label IN ('rerun','same_pipeline','unavailable')),
            status TEXT NOT NULL CHECK
            (status IN ('registered','completed','failed','excluded','abandoned')),
            reason TEXT, outcome_json TEXT, outcome_provenance TEXT,
            revision INTEGER NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            created_by_principal_id TEXT NOT NULL REFERENCES principals(id),
            CHECK ((status NOT IN ('excluded','abandoned')) OR reason IS NOT NULL));
CREATE TABLE runtime_authority_grants (
      id TEXT PRIMARY KEY, enrollment_id TEXT NOT NULL UNIQUE REFERENCES runtime_enrollments(id),
      parent_grant_id TEXT NOT NULL REFERENCES agent_authority_grants(id),
      reserved_units INTEGER NOT NULL DEFAULT 0 CHECK(reserved_units>=0),
      reserved_cents INTEGER NOT NULL DEFAULT 0 CHECK(reserved_cents>=0));
CREATE TABLE runtime_binding_revocations (
      id TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('policy','enrollment','financial')),
      target_id TEXT NOT NULL, org_id TEXT NOT NULL REFERENCES organizations(id),
      issuer_id TEXT NOT NULL REFERENCES principals(id), created_at TEXT NOT NULL,
      UNIQUE(kind,target_id));
CREATE TABLE runtime_enrollments (
          id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
          project_id TEXT NOT NULL REFERENCES projects(id),
          issuer_id TEXT NOT NULL REFERENCES principals(id),
          issuer_credential_id TEXT NOT NULL REFERENCES credentials(id),
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE runtime_financial_assertions (
          id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
          project_id TEXT NOT NULL REFERENCES projects(id),
          issuer_id TEXT NOT NULL REFERENCES principals(id),
          issuer_credential_id TEXT NOT NULL REFERENCES credentials(id),
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE runtime_native_claims (
      id TEXT PRIMARY KEY,
      operation_id TEXT NOT NULL UNIQUE REFERENCES runtime_native_operations(id),
      payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE runtime_native_entries (
      claim_id TEXT PRIMARY KEY REFERENCES runtime_native_claims(id), entered_at TEXT NOT NULL);
CREATE TABLE runtime_native_operations (
      id TEXT PRIMARY KEY, root_ref TEXT NOT NULL UNIQUE,
      enrollment_id TEXT NOT NULL REFERENCES runtime_enrollments(id),
      actor_credential_id TEXT NOT NULL REFERENCES credentials(id),
      nonce TEXT NOT NULL UNIQUE, ticket TEXT NOT NULL UNIQUE,
      payload_json TEXT NOT NULL, state TEXT NOT NULL
        CHECK(state IN ('allocated','claimed','native-returned','uncertain')),
      revision INTEGER NOT NULL CHECK(revision>0), created_at TEXT NOT NULL);
CREATE TABLE runtime_native_root_fences (
      enrollment_id TEXT PRIMARY KEY REFERENCES runtime_enrollments(id),
      operation_id TEXT NOT NULL REFERENCES runtime_native_operations(id),
      state TEXT NOT NULL CHECK(state IN ('claimed','native-returned','uncertain')));
CREATE TABLE runtime_native_settlements (
      id TEXT PRIMARY KEY, claim_id TEXT NOT NULL UNIQUE REFERENCES runtime_native_claims(id),
      actor_credential_id TEXT NOT NULL REFERENCES credentials(id),
      disposition TEXT NOT NULL CHECK(disposition IN ('native-returned','uncertain')),
      created_at TEXT NOT NULL);
CREATE TABLE runtime_policy_profiles (
          id TEXT PRIMARY KEY, org_id TEXT NOT NULL REFERENCES organizations(id),
          project_id TEXT NOT NULL REFERENCES projects(id),
          issuer_id TEXT NOT NULL REFERENCES principals(id),
          issuer_credential_id TEXT NOT NULL REFERENCES credentials(id),
          payload_json TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE schema_meta (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            schema_version INTEGER NOT NULL
        );
INSERT INTO "schema_meta" VALUES(1,62);
CREATE TABLE skill_releases (
        id TEXT PRIMARY KEY, skill_snapshot_id TEXT NOT NULL REFERENCES skill_snapshots(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id), digest TEXT NOT NULL,
        rationale TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL CHECK(revision >= 1), revoked_at TEXT);
CREATE TABLE skill_snapshots (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        name TEXT NOT NULL, description TEXT NOT NULL, digest TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        provenance TEXT NOT NULL CHECK(provenance='imported_unverified'),
        classification TEXT NOT NULL CHECK(classification='unclassified'),
        created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        UNIQUE(project_id, name, digest));
CREATE TABLE snapshot_chunk_uploads (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        expected_digest TEXT NOT NULL, manifest_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('uploading','completed')),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        created_at TEXT NOT NULL, completed_at TEXT, expires_at TEXT NOT NULL
        );
INSERT INTO "snapshot_chunk_uploads" VALUES('snapshot_aa8f35d72c4948b3866b162b45f07574','project_ba8781fd818b4e20b2189879f3967aec','234602367fcc57299510b5f8340b9dc86f0a1d1876650b02ae86f35b96b1c9f4','{"root": "synthetic", "entries": [{"relative_path": "m.py", "size": 6, "sha256": "8e33d77aa9b7bfbeecf890dec38bc479b1e1f8a4ba9ac860ae27468799a1f22d", "provenance": "imported_unverified", "classification": "unclassified"}], "exclusions": [], "total_bytes": 6}','completed','principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.504990Z','2026-11-20T19:17:29.509169Z','2026-11-21T19:17:29.505030Z');
INSERT INTO "snapshot_chunk_uploads" VALUES('snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8','project_ba8781fd818b4e20b2189879f3967aec','15618551f69b87c9ee4f4ddd5a9f9f5ece77aec6dc5c114721520a64710bfb70','{"root": "synthetic", "entries": [{"relative_path": "rows.csv", "size": 2, "sha256": "4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865", "provenance": "imported_unverified", "classification": "unclassified"}], "exclusions": [], "total_bytes": 2}','completed','principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.511507Z','2026-11-20T19:17:29.515922Z','2026-11-21T19:17:29.511542Z');
CREATE TABLE snapshot_chunks (
        owner_id TEXT NOT NULL, seq INTEGER NOT NULL CHECK(seq>=0),
        relative_path TEXT NOT NULL, offset INTEGER NOT NULL CHECK(offset>=0),
        size INTEGER NOT NULL CHECK(size BETWEEN 1 AND 4194304),
        sha256 TEXT NOT NULL CHECK(length(sha256)=64), content BLOB NOT NULL,
        PRIMARY KEY (owner_id, seq)
        );
INSERT INTO "snapshot_chunks" VALUES('snapshot_aa8f35d72c4948b3866b162b45f07574',0,'m.py',0,6,'8e33d77aa9b7bfbeecf890dec38bc479b1e1f8a4ba9ac860ae27468799a1f22d',X'6D203D20310A');
INSERT INTO "snapshot_chunks" VALUES('snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8',0,'rows.csv',0,2,'4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865',X'310A');
CREATE TABLE snapshot_data_classes (
        snapshot_id TEXT PRIMARY KEY,
        data_class TEXT NOT NULL CHECK(data_class IN ('public','private_derived')),
        origin TEXT NOT NULL
            CHECK(origin IN ('human_declared','default','derived','migrated')),
        license TEXT, source TEXT,
        actor_principal_id TEXT REFERENCES principals(id),
        decision_class TEXT, provenance_json TEXT,
        recorded_at TEXT NOT NULL,
        CHECK(data_class='private_derived' OR (origin='human_declared'
            AND license IS NOT NULL AND source IS NOT NULL
            AND actor_principal_id IS NOT NULL AND decision_class IS NOT NULL))
        );
INSERT INTO "snapshot_data_classes" VALUES('snapshot_f128bc3275b34924b75ec6951e10f5d1','private_derived','default',NULL,NULL,NULL,NULL,NULL,'2026-11-20T19:17:29.404293Z');
INSERT INTO "snapshot_data_classes" VALUES('snapshot_aa8f35d72c4948b3866b162b45f07574','private_derived','default',NULL,NULL,NULL,NULL,NULL,'2026-11-20T19:17:29.509169Z');
INSERT INTO "snapshot_data_classes" VALUES('snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8','public','human_declared','CC0-1.0','https://example.org/data','principal_12227679343446a2959e088ba1daf770','owner_automation_labelled','{"automation": true, "automation_label": "data-class-test", "credential_kind": "local_owner", "decision_class": "owner_automation_labelled"}','2026-11-20T19:17:29.511507Z');
CREATE TABLE snapshot_files (
        snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        relative_path TEXT NOT NULL, content BLOB NOT NULL,
        PRIMARY KEY (snapshot_id, relative_path)
        );
INSERT INTO "snapshot_files" VALUES('snapshot_f128bc3275b34924b75ec6951e10f5d1','input.txt',X'696D6D757461626C6520696E707574');
CREATE TABLE snapshot_releases (
        id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        actor_principal_id TEXT NOT NULL REFERENCES principals(id),
        digest TEXT NOT NULL, classification TEXT NOT NULL CHECK(classification='public'),
        rationale TEXT NOT NULL, expires_at TEXT NOT NULL, created_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK(status IN ('active','revoked')),
        revision INTEGER NOT NULL, revoked_at TEXT);
INSERT INTO "snapshot_releases" VALUES('release_91eeb00752b747cd9ec3df33cf9da7a7','snapshot_f128bc3275b34924b75ec6951e10f5d1','worker-worker-token','principal_12227679343446a2959e088ba1daf770','1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500','public','Local CPU pilot input release.','2026-11-20T19:47:29.404889+00:00','2026-11-20T19:17:29.411162Z','active',1,NULL);
CREATE TABLE source_snapshots (
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
        digest TEXT NOT NULL, manifest_json TEXT NOT NULL,
        created_at TEXT NOT NULL, actor_principal_id TEXT NOT NULL REFERENCES principals(id)
        );
INSERT INTO "source_snapshots" VALUES('snapshot_f128bc3275b34924b75ec6951e10f5d1','project_ba8781fd818b4e20b2189879f3967aec','1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500','{"root": "/fixture/workspace", "entries": [{"relative_path": "input.txt", "size": 15, "sha256": "6c731e3de9a3b004da9ee6d1a04ebf16fbd11e2e945fd3c797d80bf0b07a6ec9", "provenance": "imported_unverified", "classification": "unclassified"}], "exclusions": [], "total_bytes": 15}','2026-11-20T19:17:29.404293Z','principal_12227679343446a2959e088ba1daf770');
INSERT INTO "source_snapshots" VALUES('snapshot_aa8f35d72c4948b3866b162b45f07574','project_ba8781fd818b4e20b2189879f3967aec','234602367fcc57299510b5f8340b9dc86f0a1d1876650b02ae86f35b96b1c9f4','{"root": "synthetic", "entries": [{"relative_path": "m.py", "size": 6, "sha256": "8e33d77aa9b7bfbeecf890dec38bc479b1e1f8a4ba9ac860ae27468799a1f22d", "provenance": "imported_unverified", "classification": "unclassified"}], "exclusions": [], "total_bytes": 6}','2026-11-20T19:17:29.509169Z','principal_12227679343446a2959e088ba1daf770');
INSERT INTO "source_snapshots" VALUES('snapshot_816ff7ebb2ed428ba3fde0a43d03ddc8','project_ba8781fd818b4e20b2189879f3967aec','15618551f69b87c9ee4f4ddd5a9f9f5ece77aec6dc5c114721520a64710bfb70','{"root": "synthetic", "entries": [{"relative_path": "rows.csv", "size": 2, "sha256": "4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865", "provenance": "imported_unverified", "classification": "unclassified"}], "exclusions": [], "total_bytes": 2}','2026-11-20T19:17:29.515922Z','principal_12227679343446a2959e088ba1daf770');
CREATE TABLE task_approvals (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            plan_revision INTEGER NOT NULL,
            plan_digest TEXT NOT NULL,
            approving_principal_id TEXT NOT NULL REFERENCES principals(id),
            approved_at TEXT NOT NULL,
            PRIMARY KEY (task_id, plan_revision)
        );
INSERT INTO "task_approvals" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca',2,'0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611','principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.421527Z');
INSERT INTO "task_approvals" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca',4,'9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7','principal_12227679343446a2959e088ba1daf770','2026-11-20T19:17:29.442166Z');
CREATE TABLE task_revisions (
            task_id TEXT NOT NULL REFERENCES tasks(id),
            revision INTEGER NOT NULL,
            title TEXT NOT NULL,
            purpose TEXT NOT NULL,
            budget_cents INTEGER NOT NULL CHECK (budget_cents >= 0),
            max_duration_seconds INTEGER NOT NULL
                CHECK (max_duration_seconds BETWEEN 1 AND 86400),
            execution_spec_json TEXT,
            plan_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            actor_principal_id TEXT REFERENCES principals(id),
            PRIMARY KEY (task_id, revision)
        );
INSERT INTO "task_revisions" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca',1,'First','Exercise durable local execution authority.',100,60,NULL,'bd944782a62c84697e384d71611dd7693b23ea08f50a6465d0080a36c64e6da0','2026-11-20T19:17:29.417085Z','principal_12227679343446a2959e088ba1daf770');
INSERT INTO "task_revisions" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca',2,'First','Exercise durable local execution authority.',100,60,'{"argv":["python","-c","print(''pilot'')"],"input_snapshot_digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","max_output_bytes":1024,"network_enabled":false,"output_paths":["result.txt"],"pinned_skill_digests":[],"profile":"local-cpu"}','0df42c36c3b6ac9c9aada7d4bbe0589d660b7bc45a4418f0eb85d89e50793611','2026-11-20T19:17:29.419320Z','principal_12227679343446a2959e088ba1daf770');
INSERT INTO "task_revisions" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca',4,'First','Exercise durable local execution authority.',100,60,'{"argv":["python","-c","print(''pilot'')"],"input_snapshot_digest":"1d6c368d8390b189e54d38ea9c01a23ebebb9123960dd38e45e27f04f8087500","max_output_bytes":1024,"network_enabled":false,"output_paths":["first.txt","second.txt"],"pinned_skill_digests":[],"profile":"local-cpu"}','9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7','2026-11-20T19:17:29.436730Z','principal_12227679343446a2959e088ba1daf770');
CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL,
            purpose TEXT NOT NULL,
            budget_cents INTEGER NOT NULL CHECK (budget_cents >= 0),
            max_duration_seconds INTEGER NOT NULL
                CHECK (max_duration_seconds BETWEEN 1 AND 86400),
            status TEXT NOT NULL CHECK (status IN ('draft', 'approved')),
            revision INTEGER NOT NULL,
            current_plan_digest TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
INSERT INTO "tasks" VALUES('task_d63915ceaaac47c4a4b8f082be1b5bca','project_ba8781fd818b4e20b2189879f3967aec','First','Exercise durable local execution authority.',100,60,'approved',5,'9e51b7de849a006c07e0fcae5a7ad0a77d62285b4445fef297db6451e9d6eec7','2026-11-20T19:17:29.417085Z','2026-11-20T19:17:29.442166Z');
CREATE TABLE worker_credential_renewals (
        id TEXT PRIMARY KEY, credential_id TEXT NOT NULL REFERENCES credentials(id),
        renewed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
        renewed_by TEXT NOT NULL REFERENCES principals(id));
CREATE TABLE worker_registry (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        worker_principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id),
        credential_id TEXT NOT NULL UNIQUE REFERENCES credentials(id),
        approved_build_identifier TEXT NOT NULL,
        approved_build_digest TEXT NOT NULL,
        trust_class TEXT NOT NULL,
        region TEXT NOT NULL,
        lifecycle_state TEXT NOT NULL CHECK (lifecycle_state IN
            ('ready', 'draining', 'disconnected', 'quarantined', 'retired')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        registered_at TEXT NOT NULL,
        heartbeat_at TEXT NOT NULL,
        last_seen_at TEXT NOT NULL,
        retired_at TEXT,
        retired_by_principal_id TEXT REFERENCES principals(id)
        );
INSERT INTO "worker_registry" VALUES('registered-worker-token','org_local','project_ba8781fd818b4e20b2189879f3967aec','worker-worker-token','credential-worker-token','synthetic-test-worker','aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','test','local','ready',1,'2026-11-20T19:17:29.389435Z','2026-11-20T19:17:29.389435Z','2026-11-20T19:17:29.389435Z',NULL,NULL);
CREATE TABLE workspace_handoffs (
        id TEXT PRIMARY KEY,
        org_id TEXT NOT NULL REFERENCES organizations(id),
        project_id TEXT NOT NULL REFERENCES projects(id),
        task_id TEXT NOT NULL REFERENCES tasks(id),
        grant_id TEXT NOT NULL REFERENCES execution_grants(id),
        baseline_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(id),
        baseline_digest TEXT NOT NULL,
        workspace_label TEXT NOT NULL,
        bound_lease_id TEXT NOT NULL REFERENCES execution_leases(id),
        bound_worker_principal_id TEXT NOT NULL REFERENCES principals(id),
        bound_lease_epoch INTEGER NOT NULL CHECK (bound_lease_epoch >= 1),
        stop_epoch INTEGER NOT NULL CHECK (stop_epoch >= 1),
        status TEXT NOT NULL CHECK
            (status IN ('stop_requested', 'stopped_confirmed', 'handed_back')),
        revision INTEGER NOT NULL CHECK (revision >= 1),
        confirmation_observation_id TEXT REFERENCES execution_observations(id),
        human_owner_principal_id TEXT REFERENCES principals(id),
        result_snapshot_id TEXT REFERENCES source_snapshots(id),
        result_digest TEXT,
        changes_json TEXT,
        exclusions_json TEXT,
        context_epoch INTEGER,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
        confirmed_at TEXT, handed_back_at TEXT
        );
CREATE INDEX credentials_by_parent ON credentials(parent_id);
CREATE INDEX projects_by_org ON projects(org_id, created_at, id);
CREATE INDEX tasks_by_project ON tasks(project_id, created_at, id);
CREATE INDEX task_revisions_by_task ON task_revisions(task_id, revision);
CREATE INDEX events_by_project_cursor ON events(project_id, cursor);
CREATE INDEX research_studies_project ON research_studies(project_id, created_at, id);
CREATE INDEX research_variants_study ON research_variants(study_id, created_at, id);
CREATE INDEX research_sources_study ON research_sources(study_id, created_at, id);
CREATE INDEX research_sources_supersedes ON research_sources(supersedes_source_id);
CREATE INDEX research_findings_study ON research_findings(study_id, created_at, id);
CREATE INDEX research_corrections_study ON research_corrections(study_id, created_at, id);
CREATE INDEX external_agent_proposals_by_project ON external_agent_proposals(project_id,credential_id,created_at,id);
CREATE INDEX external_agent_contexts_by_project ON external_agent_contexts(project_id,credential_id,created_at,id);
CREATE INDEX worker_credential_renewals_by_credential ON worker_credential_renewals(credential_id,renewed_at,id);
CREATE TRIGGER worker_credential_renewals_no_update BEFORE UPDATE ON worker_credential_renewals BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER worker_credential_renewals_no_delete BEFORE DELETE ON worker_credential_renewals BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_scopes_no_update BEFORE UPDATE ON external_agent_scopes BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_scopes_no_delete BEFORE DELETE ON external_agent_scopes BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_proposals_no_update BEFORE UPDATE ON external_agent_proposals BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_proposals_no_delete BEFORE DELETE ON external_agent_proposals BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_promotions_no_update BEFORE UPDATE ON external_agent_promotions BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_promotions_no_delete BEFORE DELETE ON external_agent_promotions BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_contexts_no_update BEFORE UPDATE ON external_agent_contexts BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE TRIGGER external_agent_contexts_no_delete BEFORE DELETE ON external_agent_contexts BEGIN SELECT RAISE(ABORT, 'external agent records are immutable'); END;
CREATE INDEX dataset_snapshots_study ON dataset_snapshots(study_id,created_at,id);
CREATE INDEX dataset_ingestion_proposals_study ON dataset_ingestion_proposals(study_id,created_at,id);
CREATE INDEX interaction_threads_by_project
        ON interaction_threads(project_id, created_at, id);
CREATE INDEX interaction_messages_by_thread
        ON interaction_messages(thread_id, created_at, id);
CREATE INDEX interaction_message_deliveries_by_worker
        ON interaction_message_deliveries(worker_principal_id, state, created_at, id);
CREATE INDEX operation_harness_session_bindings_by_project
        ON operation_harness_session_bindings(project_id, created_at, id);
CREATE TRIGGER operation_harness_session_bindings_no_update
        BEFORE UPDATE ON operation_harness_session_bindings
        BEGIN SELECT RAISE(ABORT, 'operation harness session bindings are immutable'); END;
CREATE TRIGGER operation_harness_session_bindings_no_delete
        BEFORE DELETE ON operation_harness_session_bindings
        BEGIN SELECT RAISE(ABORT, 'operation harness session bindings are immutable'); END;
CREATE INDEX decision_requests_pending
        ON decision_requests(project_id, status, priority, created_at);
CREATE UNIQUE INDEX decision_requests_pending_dedup
        ON decision_requests(project_id, kind, scope_digest, task_id, expected_task_revision)
        WHERE status = 'pending';
CREATE INDEX snapshots_by_project ON source_snapshots(project_id, created_at, id);
CREATE UNIQUE INDEX active_snapshot_release
        ON snapshot_releases(snapshot_id, worker_principal_id) WHERE status='active';
CREATE UNIQUE INDEX execution_one_unresolved_grant_per_task
        ON execution_grants(task_id) WHERE status IN ('active', 'cancel_requested');
CREATE INDEX execution_grants_by_project
        ON execution_grants(project_id, created_at, id);
CREATE INDEX execution_leases_by_grant
        ON execution_leases(grant_id, expires_at, id);
CREATE INDEX execution_observations_by_grant
        ON execution_observations(grant_id, recorded_at, id);
CREATE INDEX artifacts_by_project ON artifacts(project_id, created_at, id);
CREATE INDEX artifacts_by_lease ON artifacts(lease_id, created_at, id);
CREATE INDEX artifact_uploads_by_lease ON artifact_uploads(lease_id, status, id);
CREATE INDEX artifact_manifests_by_project
        ON artifact_manifests(project_id, created_at, id);
CREATE INDEX artifact_dispositions_by_manifest ON artifact_dispositions(manifest_id, revision);
CREATE INDEX artifact_custody_releases_by_manifest ON artifact_custody_releases(manifest_id, revision);
CREATE INDEX reproduction_bundles_by_manifest ON reproduction_bundles(manifest_id, created_at, id);
CREATE INDEX worker_registry_by_project
        ON worker_registry(project_id, registered_at, id);
CREATE INDEX execution_result_checkpoints_by_project
        ON execution_result_checkpoints(project_id, committed_at, id);
CREATE INDEX skill_snapshots_by_project
        ON skill_snapshots(project_id, created_at, id);
CREATE UNIQUE INDEX active_skill_release
        ON skill_releases(skill_snapshot_id, worker_principal_id) WHERE status='active';
CREATE UNIQUE INDEX workspace_handoffs_one_open_per_task
        ON workspace_handoffs(task_id) WHERE status IN ('stop_requested', 'stopped_confirmed');
CREATE INDEX workspace_handoffs_by_project
        ON workspace_handoffs(project_id, created_at, id);
CREATE UNIQUE INDEX external_operations_experiment_attempt_once
        ON external_operations(experiment_attempt_id) WHERE experiment_attempt_id IS NOT NULL;
CREATE INDEX external_operation_receipts_by_provider
        ON external_operation_receipts(provider_kind, provider_id);
CREATE INDEX external_operations_by_project
        ON external_operations(project_id, created_at, operation_id);
CREATE INDEX execution_observations_by_operation
        ON execution_observations(operation_id, recorded_at, id);
CREATE INDEX execution_recovery_by_grant
        ON execution_recovery_reconciliations(grant_id, created_at, id);
CREATE INDEX execution_recovery_observations_by_recovery
        ON execution_recovery_observations(recovery_id, recorded_at, id);
CREATE INDEX kubernetes_dispatches_by_worker ON kubernetes_dispatches(worker_principal_id,state,created_at,id);
CREATE TRIGGER kubernetes_dispatches_fence_cancel
        AFTER UPDATE OF desired_state ON external_operations
        WHEN NEW.desired_state='cancel_requested'
        BEGIN
          UPDATE kubernetes_dispatches SET state='cancel_requested',updated_at=NEW.updated_at
          WHERE operation_id=NEW.operation_id AND state IN ('queued','claimed');
        END;
CREATE INDEX execution_result_acceptances_by_checkpoint
        ON execution_result_acceptances(checkpoint_id, revision);
CREATE INDEX credential_access_leases_by_project
        ON credential_access_leases(project_id, created_at, id);
CREATE INDEX credential_access_leases_by_execution_lease
        ON credential_access_leases(execution_lease_id, epoch, id);
CREATE UNIQUE INDEX capability_one_approval_per_plan
        ON capability_policies(task_id, plan_revision) WHERE approved_by IS NOT NULL;
CREATE INDEX project_artifact_placements_by_project ON project_artifact_placements(project_id, committed_at, artifact_id);
CREATE INDEX project_artifact_retention_by_project ON project_artifact_retention(project_id, state, activity_at, artifact_id);
CREATE INDEX research_experiments_by_project ON research_experiments(project_id,created_at,id);
CREATE INDEX research_experiment_variants_by_experiment ON research_experiment_variants(experiment_id,design_revision,id);
CREATE INDEX research_experiment_attempts_by_variant ON research_experiment_attempts(variant_id,created_at,id);
CREATE UNIQUE INDEX research_experiment_admitted_operation_once
        ON research_experiment_attempts(operation_id) WHERE provenance_kind='admitted_operation';
CREATE INDEX research_attempt_measurements_by_attempt ON research_attempt_measurements(attempt_id,created_at,id);
CREATE INDEX research_conclusions_by_experiment ON research_conclusions(experiment_id,created_at,id);
CREATE INDEX research_conclusion_amendments_by_experiment ON research_conclusion_amendments(experiment_id,created_at,id);
CREATE INDEX experiment_artifact_evidence_references_by_artifact ON experiment_artifact_evidence_references(project_id,artifact_id,id);
CREATE INDEX research_experiment_admitted_attempts_by_experiment ON research_experiment_admitted_attempts(experiment_id,created_at,id);
CREATE INDEX research_experiment_admitted_attempts_by_variant ON research_experiment_admitted_attempts(variant_id,created_at,id);
CREATE TRIGGER experiment_budget_bound_resolutions_no_update
            BEFORE UPDATE ON experiment_budget_bound_resolutions
            BEGIN SELECT RAISE(ABORT,'bound resolutions are append-only'); END;
CREATE TRIGGER experiment_budget_bound_resolutions_no_delete
            BEFORE DELETE ON experiment_budget_bound_resolutions
            BEGIN SELECT RAISE(ABORT,'bound resolutions are append-only'); END;
CREATE INDEX research_admitted_attempt_measurements_by_attempt ON research_admitted_attempt_measurements(attempt_id,created_at,id);
CREATE INDEX admitted_experiment_artifact_evidence_references_by_artifact ON admitted_experiment_artifact_evidence_references(project_id,artifact_id,id);
CREATE INDEX astra_conversation_messages_by_project ON astra_conversation_messages(project_id, conversation_revision, id);
CREATE INDEX astra_context_snapshots_by_project ON astra_selected_context_snapshots(project_id, created_at, id);
CREATE INDEX astra_coordinator_turns_by_project ON astra_coordinator_turns(project_id, state, created_at, id);
CREATE INDEX astra_proposals_by_project ON astra_proposals(project_id, created_at, id);
CREATE TRIGGER astra_context_snapshots_no_update BEFORE UPDATE ON astra_selected_context_snapshots
        BEGIN SELECT RAISE(ABORT, 'Astra selected-context snapshots are immutable'); END;
CREATE TRIGGER astra_context_snapshots_no_delete BEFORE DELETE ON astra_selected_context_snapshots
        BEGIN SELECT RAISE(ABORT, 'Astra selected-context snapshots are immutable'); END;
CREATE TRIGGER astra_proposals_no_update BEFORE UPDATE ON astra_proposals
        BEGIN SELECT RAISE(ABORT, 'Astra proposals are immutable'); END;
CREATE TRIGGER astra_proposals_no_delete BEFORE DELETE ON astra_proposals
        BEGIN SELECT RAISE(ABORT, 'Astra proposals are immutable'); END;
CREATE TRIGGER astra_conversation_messages_no_update BEFORE UPDATE ON astra_conversation_messages
        BEGIN SELECT RAISE(ABORT, 'Astra conversation messages are immutable'); END;
CREATE TRIGGER astra_conversation_messages_no_delete BEFORE DELETE ON astra_conversation_messages
        BEGIN SELECT RAISE(ABORT, 'Astra conversation messages are immutable'); END;
CREATE INDEX astra_proposal_design_registrations_by_project ON astra_proposal_design_registrations(project_id, created_at, proposal_id);
CREATE INDEX astra_coordinator_turns_claim_lease ON astra_coordinator_turns(project_id,state,claim_expires_at,created_at,id);
CREATE INDEX astra_consumer_health_by_project ON astra_consumer_health(project_id,heartbeat_at,agent_principal_id);
CREATE INDEX astra_status_discussions_by_project ON astra_status_discussions(project_id,created_at,turn_id);
CREATE INDEX astra_status_bindings_by_project ON astra_status_discussion_bindings(project_id,state,lease_expires_at,id);
CREATE INDEX astra_status_answers_by_project ON astra_status_answers(project_id,created_at,id);
CREATE TRIGGER astra_status_tool_invocations_no_update_input BEFORE UPDATE OF input_json,input_digest,tool_name ON astra_status_tool_invocations
        BEGIN SELECT RAISE(ABORT, 'Astra status tool intent is immutable'); END;
CREATE TRIGGER astra_status_answers_no_update BEFORE UPDATE ON astra_status_answers
        BEGIN SELECT RAISE(ABORT, 'Astra status answers are immutable'); END;
CREATE TRIGGER astra_status_answers_no_delete BEFORE DELETE ON astra_status_answers
        BEGIN SELECT RAISE(ABORT, 'Astra status answers are immutable'); END;
CREATE INDEX remote_workflow_runs_by_project ON remote_workflow_runs(project_id,updated_at,operation_id);
CREATE INDEX operator_audit_events_by_org_cursor
        ON operator_audit_events(org_id, cursor);
CREATE INDEX agent_authority_grants_parent
        ON agent_authority_grants(parent_id);
CREATE INDEX agent_authority_consumptions_execution
        ON agent_authority_consumptions(execution_grant_id,action);
CREATE TRIGGER agent_authority_requests_no_update BEFORE UPDATE ON agent_authority_requests BEGIN SELECT RAISE(ABORT,'agent authority history is immutable'); END;
CREATE TRIGGER agent_authority_requests_no_delete BEFORE DELETE ON agent_authority_requests BEGIN SELECT RAISE(ABORT,'agent authority history is immutable'); END;
CREATE TRIGGER agent_authority_consumptions_no_update BEFORE UPDATE ON agent_authority_consumptions BEGIN SELECT RAISE(ABORT,'agent authority history is immutable'); END;
CREATE TRIGGER agent_authority_consumptions_no_delete BEFORE DELETE ON agent_authority_consumptions BEGIN SELECT RAISE(ABORT,'agent authority history is immutable'); END;
CREATE TRIGGER output_provider_binding_no_update
            BEFORE UPDATE ON agent_output_provider_bindings BEGIN
            SELECT RAISE(ABORT, 'Output provider bindings are append-only'); END;
CREATE TRIGGER output_provider_binding_no_delete
            BEFORE DELETE ON agent_output_provider_bindings BEGIN
            SELECT RAISE(ABORT, 'Output provider bindings are append-only'); END;
CREATE INDEX agent_provenance_project_sequence
        ON agent_provenance(org_id, project_id, sequence);
CREATE TRIGGER agent_provenance_no_update
            BEFORE UPDATE ON agent_provenance BEGIN
            SELECT RAISE(ABORT, 'Agent provenance is append-only'); END;
CREATE TRIGGER agent_provenance_no_delete
            BEFORE DELETE ON agent_provenance BEGIN
            SELECT RAISE(ABORT, 'Agent provenance is append-only'); END;
CREATE INDEX agent_research_project ON agent_research_records(org_id,project_id,sequence);
CREATE TRIGGER agent_research_records_no_update BEFORE UPDATE ON agent_research_records BEGIN SELECT RAISE(ABORT,'agent research history is immutable'); END;
CREATE TRIGGER agent_research_records_no_delete BEFORE DELETE ON agent_research_records BEGIN SELECT RAISE(ABORT,'agent research history is immutable'); END;
CREATE TRIGGER agent_research_promotions_no_update BEFORE UPDATE ON agent_research_promotions BEGIN SELECT RAISE(ABORT,'agent research history is immutable'); END;
CREATE TRIGGER agent_research_promotions_no_delete BEFORE DELETE ON agent_research_promotions BEGIN SELECT RAISE(ABORT,'agent research history is immutable'); END;
CREATE UNIQUE INDEX generated_output_source_identity
        ON generated_public_outputs(grant_id,json_extract(metadata_json,'$.source_event_id'),
        json_extract(metadata_json,'$.source_sequence'));
CREATE TRIGGER generated_output_no_update
            BEFORE UPDATE ON generated_public_outputs BEGIN
            SELECT RAISE(ABORT,'Generated outputs are immutable'); END;
CREATE TRIGGER generated_output_no_delete
            BEFORE DELETE ON generated_public_outputs BEGIN
            SELECT RAISE(ABORT,'Generated outputs are immutable'); END;
CREATE TRIGGER runtime_policy_profiles_no_update BEFORE UPDATE ON runtime_policy_profiles BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_policy_profiles_no_delete BEFORE DELETE ON runtime_policy_profiles BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_enrollments_no_update BEFORE UPDATE ON runtime_enrollments BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_enrollments_no_delete BEFORE DELETE ON runtime_enrollments BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_financial_assertions_no_update BEFORE UPDATE ON runtime_financial_assertions BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_financial_assertions_no_delete BEFORE DELETE ON runtime_financial_assertions BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_binding_revocations_no_update BEFORE UPDATE ON runtime_binding_revocations BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_binding_revocations_no_delete BEFORE DELETE ON runtime_binding_revocations BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_native_claims_no_update BEFORE UPDATE ON runtime_native_claims BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_native_claims_no_delete BEFORE DELETE ON runtime_native_claims BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_native_settlements_no_update BEFORE UPDATE ON runtime_native_settlements BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_native_settlements_no_delete BEFORE DELETE ON runtime_native_settlements BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_native_entries_no_update BEFORE UPDATE ON runtime_native_entries BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE TRIGGER runtime_native_entries_no_delete BEFORE DELETE ON runtime_native_entries BEGIN SELECT RAISE(ABORT,'runtime binding history is immutable'); END;
CREATE INDEX execution_run_receipts_by_project
        ON execution_run_receipts(project_id, created_at, operation_id);
CREATE TRIGGER execution_run_receipts_append_only_update
        BEFORE UPDATE ON execution_run_receipts
        BEGIN SELECT RAISE(ABORT, 'execution run receipts are immutable'); END;
CREATE TRIGGER execution_run_receipts_append_only_delete
        BEFORE DELETE ON execution_run_receipts
        BEGIN SELECT RAISE(ABORT, 'execution run receipts are immutable'); END;
CREATE TRIGGER execution_run_receipts_exclude_fake
        BEFORE INSERT ON execution_run_receipts
        WHEN EXISTS (SELECT 1 FROM external_operation_receipts WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT, 'operation already has fake provider evidence'); END;
CREATE TRIGGER external_operation_receipts_exclude_run
        BEFORE INSERT ON external_operation_receipts
        WHEN EXISTS (SELECT 1 FROM execution_run_receipts WHERE operation_id=NEW.operation_id)
        BEGIN SELECT RAISE(ABORT, 'operation already has observed provider evidence'); END;
CREATE INDEX execution_observed_result_acceptances_by_checkpoint
        ON execution_observed_result_acceptances(checkpoint_id, revision);
CREATE INDEX artifact_lineage_nodes_by_project
        ON artifact_lineage_nodes(project_id, created_at, id);
CREATE INDEX artifact_lineage_edges_by_parent
        ON artifact_lineage_edges(parent_node_id, child_node_id);
CREATE INDEX artifact_lineage_edges_by_project
        ON artifact_lineage_edges(project_id, created_at, id);
CREATE TRIGGER artifact_lineage_edges_same_project
        BEFORE INSERT ON artifact_lineage_edges
        WHEN (SELECT count(*) FROM artifact_lineage_nodes
              WHERE id IN (NEW.child_node_id, NEW.parent_node_id)
              AND project_id=NEW.project_id AND org_id=NEW.org_id) <> 2
        BEGIN SELECT RAISE(ABORT, 'lineage edge endpoints must share its project'); END;
CREATE TRIGGER artifact_lineage_nodes_append_only_update
                BEFORE UPDATE ON artifact_lineage_nodes
                BEGIN SELECT RAISE(ABORT, 'artifact lineage records are append-only'); END;
CREATE TRIGGER artifact_lineage_nodes_append_only_delete
                BEFORE DELETE ON artifact_lineage_nodes
                BEGIN SELECT RAISE(ABORT, 'artifact lineage records are append-only'); END;
CREATE TRIGGER artifact_lineage_edges_append_only_update
                BEFORE UPDATE ON artifact_lineage_edges
                BEGIN SELECT RAISE(ABORT, 'artifact lineage records are append-only'); END;
CREATE TRIGGER artifact_lineage_edges_append_only_delete
                BEFORE DELETE ON artifact_lineage_edges
                BEGIN SELECT RAISE(ABORT, 'artifact lineage records are append-only'); END;
CREATE INDEX gate_split_lock_members_by_key ON gate_split_lock_members(lock_id, member_key);
CREATE INDEX gate_split_lock_members_by_group ON gate_split_lock_members(lock_id, group_key);
CREATE INDEX gate_leakage_audits_by_lock ON gate_leakage_audits(lock_id, created_at, id);
CREATE INDEX gate_metric_records_by_series ON gate_metric_records(variant_id, lock_id, metric, partition, seed);
CREATE INDEX provider_input_tickets_by_operation ON provider_input_tickets(operation_id);
CREATE TRIGGER snapshot_data_classes_no_update
        BEFORE UPDATE ON snapshot_data_classes
        BEGIN SELECT RAISE(ABORT, 'snapshot data class is immutable'); END;
CREATE TRIGGER snapshot_data_classes_no_delete
        BEFORE DELETE ON snapshot_data_classes
        WHEN EXISTS (SELECT 1 FROM source_snapshots WHERE id = OLD.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot data class is immutable'); END;
CREATE TRIGGER snapshot_data_classes_no_replace
        BEFORE INSERT ON snapshot_data_classes
        WHEN EXISTS (SELECT 1 FROM snapshot_data_classes WHERE snapshot_id = NEW.snapshot_id)
        BEGIN SELECT RAISE(ABORT, 'snapshot data class is immutable'); END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',21);
COMMIT;
