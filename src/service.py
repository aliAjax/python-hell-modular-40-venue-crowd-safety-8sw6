from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import (
    TRANSFER_DISPATCHABLE,
    TRANSFER_HELD_STATUSES,
    RuleEngine,
    _team_available,
    assert_bed_capacity,
    medical_point_open,
)

# Compound actions that span multiple entities and must run in one unit of work.
COMPOUND_TRANSFERS = {"dispatch", "receive", "release", "void", "reselect"}


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    # ------------------------------------------------------------------ create

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        if kind == "transfer":
            return self.create_transfer(actor, data or {}, idempotency_key)
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

    # ------------------------------------------------------------- transitions

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "transfer" and action in COMPOUND_TRANSFERS:
            method = {
                "dispatch": self.dispatch_transfer,
                "receive": self.receive_transfer,
                "release": self.release_transfer,
                "void": self.void_transfer,
                "reselect": self.reselect_transfer,
            }[action]
            return method(
                actor, entity_id, dict(data or {}), expected_version=expected_version
            )
        if kind == "medical_point" and action in ("mark_full", "close"):
            return self.transition_medical_point(
                actor, entity_id, action, dict(data or {}), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
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

    # ------------------------------------------------------------ transfer: 预占

    def create_transfer(self, actor, data, idempotency_key=None):
        """Create the single transfer order for a triaged incident and hold a bed."""
        payload = dict(data or {})
        if idempotency_key:
            existing_id = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing_id and self.repository.get_entity(existing_id):
                return self.repository.get_entity(existing_id)
        validated = self.rules.validate_create(actor, "transfer", payload, self._lookup)
        payload.update(validated)
        transfer_id = str(payload.pop("id", "") or uuid4())
        incident_id = payload["incident_id"]
        point_id = payload["medical_point_id"]
        beds = int(payload.get("beds", 1))
        payload["reserved_by"] = actor.user_id

        def work(tx):
            if idempotency_key:
                existing_id = tx.get_idempotency(actor.user_id, idempotency_key)
                if existing_id and tx.get(existing_id):
                    return existing_id
            if tx.get(transfer_id):
                raise ConflictError("entity already exists: " + transfer_id)
            incident = tx.get(incident_id)
            point = tx.get(point_id)
            # Re-check inside the write lock: another commander may have just
            # won the last bed or opened a transfer order for this incident.
            if not incident or incident["status"] != "triaged":
                raise ConflictError("incident must be triaged before a bed is reserved")
            if not medical_point_open(point):
                raise ConflictError("medical point is not accepting transfers")
            for transfer in tx.list("transfer"):
                if transfer["data"].get("incident_id") != incident_id:
                    continue
                if transfer["status"] in ("reserved", "dispatched", "released", "void"):
                    raise ConflictError("incident already has transfer order " + transfer["id"])
            assert_bed_capacity(
                point, _tx_lookup(tx), extra=beds, ignore_transfer_id=transfer_id
            )
            point_data = dict(point["data"])
            point_data["beds_held"] = int(point_data.get("beds_held", 0)) + beds
            tx.update(point["id"], point["version"], point["status"], point_data)
            tx.insert(transfer_id, "transfer", "reserved", payload, actor.user_id)
            tx.audit(transfer_id, actor, "create", None, "reserved", {"kind": "transfer"})
            tx.audit(
                point["id"], actor, "hold_bed", point["status"], point["status"],
                {"transfer_id": transfer_id, "beds": beds},
            )
            if idempotency_key:
                tx.save_idempotency(actor.user_id, idempotency_key, transfer_id)
            return transfer_id

        saved_id = self.repository.run_in_transaction(work)
        return self.repository.get_entity(saved_id)

    # ----------------------------------------------------- transfer: 派发与重试

    def dispatch_transfer(self, actor, transfer_id, data, expected_version=None):
        """Dispatch the on-site task for a reserved (or released-on-retry) order."""
        transfer = self.get(transfer_id)
        expected = int(expected_version) if expected_version is not None else transfer["version"]
        team_id = data.get("team_id")
        commander_id = data.get("commander_id") or actor.user_id
        from_status = transfer["status"]
        incident = self.repository.get_entity(transfer["data"]["incident_id"])

        def work(tx):
            tx_transfer = tx.get(transfer_id)
            if tx_transfer is None:
                raise NotFoundError("entity not found: " + transfer_id)
            if tx_transfer["version"] != expected:
                raise ConflictError("version conflict on transfer order")
            if tx_transfer["status"] not in TRANSFER_DISPATCHABLE:
                raise InvalidTransition(
                    "cannot dispatch transfer from status %s" % tx_transfer["status"]
                )
            lookup = _tx_lookup(tx)
            _team_available(lookup, team_id)
            tx_point = tx.get(tx_transfer["data"]["medical_point_id"])
            if not medical_point_open(tx_point):
                raise ConflictError("medical point is not accepting transfers")
            beds = int(tx_transfer["data"].get("beds", 1))
            if tx_transfer["status"] == "released":
                # Retry on the same order re-acquires its bed exactly once.
                assert_bed_capacity(tx_point, lookup, beds, transfer_id)
                point_data = dict(tx_point["data"])
                point_data["beds_held"] = int(point_data.get("beds_held", 0)) + beds
                tx.update(tx_point["id"], tx_point["version"], tx_point["status"], point_data)
                tx.audit(
                    tx_point["id"], actor, "hold_bed", tx_point["status"], tx_point["status"],
                    {"transfer_id": transfer_id, "beds": beds, "retry": True},
                )

            tx_incident = tx.get(tx_transfer["data"]["incident_id"])
            task_data = {
                "incident_id": tx_transfer["data"]["incident_id"],
                "venue_id": tx_transfer["data"]["venue_id"],
                "zone_id": tx_incident["data"]["zone_id"],
                "team_id": team_id,
                "task_type": "medical_transfer",
                "transfer_id": transfer_id,
                "commander_id": commander_id,
            }
            task_id = str(uuid4())
            tx.insert(task_id, "task", "draft", task_data, actor.user_id)
            tx.audit(task_id, actor, "create", None, "draft", {"kind": "task"})
            tx.update(task_id, 1, "assigned", dict(task_data, assigned_by=actor.user_id))
            tx.audit(task_id, actor, "assign", "draft", "assigned", {"team_id": team_id})

            attempts = int(tx_transfer["data"].get("attempts", 0)) + 1
            transfer_data = dict(tx_transfer["data"])
            transfer_data.update(
                {"team_id": team_id, "task_id": task_id, "attempts": attempts}
            )
            tx.update(transfer_id, expected, "dispatched", transfer_data)
            tx.audit(
                transfer_id, actor, "dispatch", tx_transfer["status"], "dispatched",
                {"team_id": team_id, "task_id": task_id, "attempt": attempts},
            )

            if tx_incident and tx_incident["status"] == "triaged":
                incident_data = dict(tx_incident["data"])
                incident_data["active_transfer_id"] = transfer_id
                tx.update(tx_incident["id"], tx_incident["version"], "dispatched", incident_data)
                tx.audit(
                    tx_incident["id"], actor, "dispatch", "triaged", "dispatched",
                    {"transfer_id": transfer_id, "commander_id": commander_id},
                )
            return True

        try:
            # Role, required fields, state and treatment-team availability are
            # checked here; authoritative re-checks run inside the write lock.
            self.rules.validate_transition(
                actor, transfer, "dispatch", dict(data), self._lookup
            )
            point = self.repository.get_entity(transfer["data"]["medical_point_id"])
            if not medical_point_open(point):
                raise ConflictError("medical point is not accepting transfers")
            if from_status == "released":
                assert_bed_capacity(point, self._lookup, transfer["data"]["beds"], transfer_id)
            self.repository.run_in_transaction(work)
        except Exception:
            # Task dispatch failed while the order still held a bed: return it so
            # a retry (or another incident) can use it. The same order stays open.
            if from_status == "reserved":
                try:
                    self.release_transfer(
                        actor,
                        transfer_id,
                        {"reason": "dispatch failed; bed returned for retry"},
                    )
                except Exception:
                    pass
            raise
        return self.repository.get_entity(transfer_id)

    # -------------------------------------------------------- transfer: 接班接收

    def receive_transfer(self, actor, transfer_id, data, expected_version=None):
        transfer = self.get(transfer_id)
        expected = int(expected_version) if expected_version is not None else transfer["version"]
        self.rules.validate_transition(
            actor, transfer, "receive", dict(data), self._lookup
        )
        receiver_id = data.get("receiver_id") or actor.user_id
        received_at = data.get("received_at")

        def work(tx):
            tx_transfer = tx.get(transfer_id)
            if not tx_transfer:
                raise NotFoundError("entity not found: " + transfer_id)
            if tx_transfer["version"] != expected:
                raise ConflictError("version conflict on transfer order")
            if tx_transfer["status"] != "dispatched":
                raise InvalidTransition(
                    "cannot receive transfer from status %s" % tx_transfer["status"]
                )
            point = tx.get(tx_transfer["data"]["medical_point_id"])
            if not medical_point_open(point):
                raise ConflictError("medical point is not accepting transfers")
            beds = int(tx_transfer["data"].get("beds", 1))
            point_data = dict(point["data"])
            # Only on handover confirmation does a held bed become an admission.
            point_data["beds_held"] = max(0, int(point_data.get("beds_held", 0)) - beds)
            point_data["patients"] = int(point_data.get("patients", 0)) + beds
            tx.update(point["id"], point["version"], point["status"], point_data)
            tx.audit(
                point["id"], actor, "admit_patient", point["status"], point["status"],
                {"transfer_id": transfer_id, "beds": beds},
            )
            transfer_data = dict(tx_transfer["data"])
            transfer_data.update({"receiver_id": receiver_id, "received_at": received_at})
            tx.update(transfer_id, expected, "received", transfer_data)
            tx.audit(
                transfer_id, actor, "receive", "dispatched", "received",
                {"receiver_id": receiver_id, "received_at": received_at},
            )
            return True

        self.repository.run_in_transaction(work)
        return self.repository.get_entity(transfer_id)

    def release_transfer(self, actor, transfer_id, data, expected_version=None):
        """Return the held bed and keep the order open for a retry (system/commander)."""
        transfer = self.get(transfer_id)
        reason = data.get("reason") or "bed released"
        self.rules.validate_transition(
            actor, transfer, "release", {"reason": reason}, self._lookup
        )

        def work(tx):
            tx_transfer = tx.get(transfer_id)
            if not tx_transfer or tx_transfer["status"] not in TRANSFER_HELD_STATUSES:
                raise InvalidTransition("transfer has no held bed to release")
            point = tx.get(tx_transfer["data"]["medical_point_id"])
            beds = int(tx_transfer["data"].get("beds", 1))
            if point:
                point_data = dict(point["data"])
                point_data["beds_held"] = max(
                    0, int(point_data.get("beds_held", 0)) - beds
                )
                tx.update(point["id"], point["version"], point["status"], point_data)
                tx.audit(
                    point["id"], actor, "release_bed", point["status"], point["status"],
                    {"transfer_id": transfer_id, "beds": beds},
                )
            transfer_data = dict(tx_transfer["data"])
            transfer_data["release_reason"] = reason
            tx.update(transfer_id, tx_transfer["version"], "released", transfer_data)
            tx.audit(
                transfer_id, actor, "release", tx_transfer["status"], "released",
                {"reason": reason},
            )
            return True

        self.repository.run_in_transaction(work)
        return self.repository.get_entity(transfer_id)

    def void_transfer(self, actor, transfer_id, data, expected_version=None):
        """Invalidate an un-received order (used when its medical point changes)."""
        transfer = self.get(transfer_id)
        reason = data.get("reason") or "medical point status changed"
        self.rules.validate_transition(
            actor, transfer, "void", {"reason": reason}, self._lookup
        )

        def work(tx):
            tx_transfer = tx.get(transfer_id)
            if not tx_transfer:
                raise NotFoundError("entity not found: " + transfer_id)
            if tx_transfer["status"] not in ("reserved", "dispatched", "released"):
                raise InvalidTransition("transfer cannot be voided")
            from_status = tx_transfer["status"]
            point = tx.get(tx_transfer["data"]["medical_point_id"])
            beds = int(tx_transfer["data"].get("beds", 1))
            if point and from_status in TRANSFER_HELD_STATUSES:
                point_data = dict(point["data"])
                point_data["beds_held"] = max(
                    0, int(point_data.get("beds_held", 0)) - beds
                )
                tx.update(point["id"], point["version"], point["status"], point_data)
                tx.audit(
                    point["id"], actor, "release_bed", point["status"], point["status"],
                    {"transfer_id": transfer_id, "beds": beds, "voided": True},
                )
            transfer_data = dict(tx_transfer["data"])
            transfer_data["void_reason"] = reason
            tx.update(transfer_id, tx_transfer["version"], "void", transfer_data)
            tx.audit(transfer_id, actor, "void", from_status, "void", {"reason": reason})
            return True

        self.repository.run_in_transaction(work)
        return self.repository.get_entity(transfer_id)

    def reselect_transfer(self, actor, transfer_id, data, expected_version=None):
        """Commander re-picks a medical point on the same (voided) transfer order."""
        transfer = self.get(transfer_id)
        expected = int(expected_version) if expected_version is not None else transfer["version"]
        new_point_id = data.get("medical_point_id")
        self.rules.validate_transition(
            actor, transfer, "reselect", {"medical_point_id": new_point_id}, self._lookup
        )
        beds = int(transfer["data"].get("beds", 1))

        def work(tx):
            tx_transfer = tx.get(transfer_id)
            if not tx_transfer:
                raise NotFoundError("entity not found: " + transfer_id)
            if tx_transfer["version"] != expected:
                raise ConflictError("version conflict on transfer order")
            if tx_transfer["status"] != "void":
                raise InvalidTransition("only a voided transfer can be reselected")
            point = tx.get(new_point_id)
            if not medical_point_open(point):
                raise ConflictError("medical point is not accepting transfers")
            assert_bed_capacity(point, _tx_lookup(tx), beds, transfer_id)
            point_data = dict(point["data"])
            point_data["beds_held"] = int(point_data.get("beds_held", 0)) + beds
            tx.update(point["id"], point["version"], point["status"], point_data)
            tx.audit(
                point["id"], actor, "hold_bed", point["status"], point["status"],
                {"transfer_id": transfer_id, "beds": beds, "reselect": True},
            )
            transfer_data = dict(tx_transfer["data"])
            history = list(transfer_data.get("medical_point_history") or [])
            history.append(
                {"medical_point_id": tx_transfer["data"]["medical_point_id"],
                 "reason": tx_transfer["data"].get("void_reason")}
            )
            transfer_data["medical_point_id"] = new_point_id
            transfer_data["medical_point_history"] = history
            transfer_data.pop("void_reason", None)
            transfer_data["reselected_by"] = actor.user_id
            tx.update(transfer_id, expected, "reserved", transfer_data)
            tx.audit(
                transfer_id, actor, "reselect", "void", "reserved",
                {"medical_point_id": new_point_id},
            )
            return True

        self.repository.run_in_transaction(work)
        return self.repository.get_entity(transfer_id)

    # --------------------------------------------------- medical point cascade

    def transition_medical_point(self, actor, point_id, action, data, expected_version=None):
        point = self.get(point_id)
        expected = int(expected_version) if expected_version is not None else point["version"]
        next_status, patch = self.rules.validate_transition(
            actor, point, action, data, self._lookup
        )

        def work(tx):
            tx_point = tx.get(point_id)
            if not tx_point:
                raise NotFoundError("entity not found: " + point_id)
            if tx_point["version"] != expected:
                raise ConflictError("version conflict on medical point")
            merged = dict(tx_point["data"])
            merged.update(patch)
            # Every un-received order is invalidated and gives its bed back.
            pending = [
                tx.get(t["id"])
                for t in tx.list("transfer")
                if t["data"].get("medical_point_id") == point_id
                and t["status"] in ("reserved", "dispatched", "released")
            ]
            released = sum(
                int(t["data"].get("beds", 1))
                for t in pending
                if t and t["status"] in TRANSFER_HELD_STATUSES
            )
            if released:
                merged["beds_held"] = max(0, int(merged.get("beds_held", 0)) - released)
            tx.update(point_id, expected, next_status, merged)
            tx.audit(point_id, actor, action, tx_point["status"], next_status, {"patch": patch})
            reason = data.get("reason") or ("medical point " + next_status)
            for tx_transfer in pending:
                if not tx_transfer:
                    continue
                transfer_data = dict(tx_transfer["data"])
                transfer_data["void_reason"] = reason
                tx.update(tx_transfer["id"], tx_transfer["version"], "void", transfer_data)
                tx.audit(
                    tx_transfer["id"], actor, "void", tx_transfer["status"], "void",
                    {"reason": reason, "medical_point_status": next_status},
                )
            return True

        self.repository.run_in_transaction(work)
        return self.repository.get_entity(point_id)


def _tx_lookup(tx):
    """Build the same ``lookup(kind, field, value)`` callable over a transaction."""
    def lookup(kind, field, value):
        return [
            entity
            for entity in tx.list(kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    return lookup
