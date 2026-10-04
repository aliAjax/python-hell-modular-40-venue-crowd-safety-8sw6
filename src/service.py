from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError, ValidationError
from .repository import utcnow
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "transfer":
            return self._create_transfer(actor, data, idempotency_key)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        validated = self.rules.validate_create(actor, kind, payload, self._lookup)
        if validated:
            payload.update(validated)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def _create_transfer(self, actor, data, idempotency_key=None):
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, "transfer", payload, self._lookup)
        transfer_id = str(payload.pop("id", "") or uuid4())
        entity = self.repository.create_transfer_atomic(
            transfer_id,
            payload["incident_id"],
            payload["medical_point_id"],
            actor.user_id,
        )
        self.audit.record(
            transfer_id,
            actor,
            "create",
            None,
            "reserved",
            {
                "kind": "transfer",
                "incident_id": payload["incident_id"],
                "medical_point_id": payload["medical_point_id"],
            },
        )
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, transfer_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        if entity["kind"] == "transfer":
            return self._transition_transfer(actor, entity, action, data, expected_version)
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        if entity["kind"] == "medical_point" and action in ("mark_full", "close"):
            updated = self.repository.transition_medical_point(
                entity_id, expected, next_status, merged, void_transfers=True
            )
        else:
            updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def _transition_transfer(self, actor, transfer, action, data, expected_version):
        if action == "dispatch" and transfer["status"] == "dispatched":
            return transfer
        next_status, patch = self.rules.validate_transition(
            actor, transfer, action, dict(data or {}), self._lookup
        )
        if action == "dispatch":
            return self._dispatch_transfer(actor, transfer, data or {}, expected_version)
        if action == "receive":
            return self._receive_transfer(actor, transfer, expected_version)
        if action == "void":
            return self._void_transfer(actor, transfer, expected_version)
        raise InvalidTransition("unknown transfer action: " + action)

    def _dispatch_transfer(self, actor, transfer, data, expected_version):
        team_id = data.get("team_id")
        task_type = data.get("task_type")
        incident = self.repository.get_entity(transfer["data"]["incident_id"])
        if not incident:
            raise NotFoundError("incident not found: " + transfer["data"]["incident_id"])
        if not transfer["data"].get("bed_held"):
            transfer = self.repository.reserve_bed_for_transfer(
                transfer["id"], transfer["data"]["medical_point_id"]
            )
        task_id = transfer["data"].get("task_id")
        if not task_id:
            task = self.create(
                actor,
                "task",
                {
                    "incident_id": incident["id"],
                    "venue_id": incident["data"]["venue_id"],
                    "zone_id": incident["data"]["zone_id"],
                    "team_id": team_id,
                    "task_type": task_type,
                },
            )
            task_id = task["id"]
        try:
            self.transition(
                actor,
                task_id,
                "assign",
                {"assigned_at": data.get("assigned_at") or utcnow()},
            )
        except ConflictError:
            self.repository.release_bed_for_transfer(
                transfer["id"], transfer["data"]["medical_point_id"]
            )
            raise
        merged = dict(transfer["data"])
        merged["task_id"] = task_id
        merged["dispatched_at"] = utcnow()
        updated = self.repository.update_entity(
            transfer["id"], transfer["version"], "dispatched", merged
        )
        self.audit.record(
            transfer["id"],
            actor,
            "dispatch",
            transfer["status"],
            "dispatched",
            {"task_id": task_id},
        )
        return updated

    def _receive_transfer(self, actor, transfer, expected_version):
        updated = self.repository.receive_transfer_atomic(
            transfer["id"], transfer["data"]["medical_point_id"]
        )
        self.audit.record(
            transfer["id"], actor, "receive", transfer["status"], "received", {}
        )
        return updated

    def _void_transfer(self, actor, transfer, expected_version):
        updated = self.repository.void_transfer_atomic(
            transfer["id"], transfer["data"]["medical_point_id"]
        )
        self.audit.record(
            transfer["id"], actor, "void", transfer["status"], "void", {}
        )
        return updated

    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
