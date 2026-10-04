import json
import sqlite3
from datetime import datetime, timezone

from .domain import ConflictError, NotFoundError


def utcnow():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SQLiteRepository:
    def __init__(self, path):
        self.path = str(path)
        self._initialize()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self):
        with self._connect() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS entities (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    data TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_entities_kind_status
                    ON entities(kind, status);
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    actor_role TEXT NOT NULL,
                    action TEXT NOT NULL,
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_audit_entity
                    ON audit_log(entity_id, id);
                CREATE TABLE IF NOT EXISTS idempotency (
                    actor_id TEXT NOT NULL,
                    idem_key TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(actor_id, idem_key)
                );
            """)

    @staticmethod
    def _entity_from_row(row):
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "version": int(row["version"]),
            "data": json.loads(row["data"]),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_entity(self, entity_id, kind, status, data, actor_id):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
                (entity_id, kind, status, payload, actor_id, now, now),
            )
        return self.get_entity(entity_id)

    def get_entity(self, entity_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
        return self._entity_from_row(row) if row else None

    def list_entities(self, kind=None, status=None):
        clauses = []
        params = []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM entities" + where + " ORDER BY created_at, id", params
            ).fetchall()
        return [self._entity_from_row(row) for row in rows]

    def find_entities(self, kind, field, value):
        return [
            entity
            for entity in self.list_entities(kind=kind)
            if (entity["id"] == value if field == "id" else entity["data"].get(field) == value)
        ]

    def update_entity(self, entity_id, expected_version, status, data):
        now = utcnow()
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT version FROM entities WHERE id = ?", (entity_id,)
            ).fetchone()
            if not row:
                raise NotFoundError("entity not found: " + entity_id)
            current_version = int(row["version"])
            if expected_version is not None and current_version != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, current_version)
                )
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? "
                "WHERE id = ? AND version = ?",
                (status, payload, now, entity_id, current_version),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(entity_id)

    def append_audit(self, entity_id, actor_id, actor_role, action, from_status, to_status, detail):
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO audit_log(entity_id, actor_id, actor_role, action, from_status, to_status, detail, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    entity_id,
                    actor_id,
                    actor_role,
                    action,
                    from_status,
                    to_status,
                    json.dumps(detail, ensure_ascii=False, sort_keys=True),
                    utcnow(),
                ),
            )

    def list_audit(self, entity_id=None):
        with self._connect() as connection:
            if entity_id:
                rows = connection.execute(
                    "SELECT * FROM audit_log WHERE entity_id = ? ORDER BY id", (entity_id,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM audit_log ORDER BY id").fetchall()
        return [
            {
                "id": row["id"],
                "entity_id": row["entity_id"],
                "actor_id": row["actor_id"],
                "actor_role": row["actor_role"],
                "action": row["action"],
                "from_status": row["from_status"],
                "to_status": row["to_status"],
                "detail": json.loads(row["detail"]),
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    def get_idempotency(self, actor_id, idem_key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT entity_id FROM idempotency WHERE actor_id = ? AND idem_key = ?",
                (actor_id, idem_key),
            ).fetchone()
        return row["entity_id"] if row else None

    def save_idempotency(self, actor_id, idem_key, entity_id):
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO idempotency(actor_id, idem_key, entity_id, created_at) "
                "VALUES (?, ?, ?, ?)",
                (actor_id, idem_key, entity_id, utcnow()),
            )

    def ping(self):
        with self._connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return True

    @staticmethod
    def _load_medical_point(row):
        data = json.loads(row["data"])
        return (
            int(data.get("capacity", 0)),
            int(data.get("patients", 0)),
            int(data.get("reserved", 0)),
        )

    def _write_medical_point(self, connection, medical_point_id, data):
        now = utcnow()
        connection.execute(
            "UPDATE entities SET version = version + 1, data = ?, updated_at = ? WHERE id = ?",
            (json.dumps(data, ensure_ascii=False, sort_keys=True), now, medical_point_id),
        )

    def _write_transfer(self, connection, transfer_id, status, data):
        now = utcnow()
        connection.execute(
            "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? WHERE id = ?",
            (status, json.dumps(data, ensure_ascii=False, sort_keys=True), now, transfer_id),
        )

    def create_transfer_atomic(self, transfer_id, incident_id, medical_point_id, commander_id):
        now = utcnow()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mp_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (medical_point_id,)
            ).fetchone()
            if not mp_row:
                raise NotFoundError("medical point not found: " + medical_point_id)
            if mp_row["status"] != "active":
                raise ConflictError("medical point is not accepting patients")
            capacity, patients, reserved = self._load_medical_point(mp_row)
            if capacity - patients - reserved <= 0:
                raise ConflictError("no available beds: bed occupied")
            active_rows = connection.execute(
                "SELECT id, data FROM entities WHERE kind = 'transfer' "
                "AND status IN ('reserved', 'dispatched')",
            ).fetchall()
            for row in active_rows:
                if json.loads(row["data"]).get("incident_id") == incident_id:
                    raise ConflictError("incident already has an active transfer")
            mp_data = json.loads(mp_row["data"])
            mp_data["reserved"] = reserved + 1
            self._write_medical_point(connection, medical_point_id, mp_data)
            transfer_data = {
                "incident_id": incident_id,
                "medical_point_id": medical_point_id,
                "task_id": None,
                "commander_id": commander_id,
                "bed_held": True,
            }
            connection.execute(
                "INSERT INTO entities(id, kind, status, version, data, created_by, created_at, updated_at) "
                "VALUES (?, 'transfer', 'reserved', 1, ?, ?, ?, ?)",
                (
                    transfer_id,
                    json.dumps(transfer_data, ensure_ascii=False, sort_keys=True),
                    commander_id,
                    now,
                    now,
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(transfer_id)

    def reserve_bed_for_transfer(self, transfer_id, medical_point_id):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mp_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (medical_point_id,)
            ).fetchone()
            if not mp_row:
                raise NotFoundError("medical point not found: " + medical_point_id)
            capacity, patients, reserved = self._load_medical_point(mp_row)
            if capacity - patients - reserved <= 0:
                raise ConflictError("no available beds: bed occupied")
            mp_data = json.loads(mp_row["data"])
            mp_data["reserved"] = reserved + 1
            self._write_medical_point(connection, medical_point_id, mp_data)
            t_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (transfer_id,)
            ).fetchone()
            if not t_row:
                raise NotFoundError("transfer not found: " + transfer_id)
            tdata = json.loads(t_row["data"])
            tdata["bed_held"] = True
            self._write_transfer(connection, transfer_id, t_row["status"], tdata)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(transfer_id)

    def release_bed_for_transfer(self, transfer_id, medical_point_id):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mp_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (medical_point_id,)
            ).fetchone()
            if not mp_row:
                raise NotFoundError("medical point not found: " + medical_point_id)
            _, _, reserved = self._load_medical_point(mp_row)
            mp_data = json.loads(mp_row["data"])
            mp_data["reserved"] = max(0, reserved - 1)
            self._write_medical_point(connection, medical_point_id, mp_data)
            t_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (transfer_id,)
            ).fetchone()
            if not t_row:
                raise NotFoundError("transfer not found: " + transfer_id)
            tdata = json.loads(t_row["data"])
            tdata["bed_held"] = False
            self._write_transfer(connection, transfer_id, t_row["status"], tdata)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(transfer_id)

    def receive_transfer_atomic(self, transfer_id, medical_point_id):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mp_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (medical_point_id,)
            ).fetchone()
            if not mp_row:
                raise NotFoundError("medical point not found: " + medical_point_id)
            _, patients, reserved = self._load_medical_point(mp_row)
            mp_data = json.loads(mp_row["data"])
            mp_data["reserved"] = max(0, reserved - 1)
            mp_data["patients"] = patients + 1
            self._write_medical_point(connection, medical_point_id, mp_data)
            t_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (transfer_id,)
            ).fetchone()
            if not t_row:
                raise NotFoundError("transfer not found: " + transfer_id)
            tdata = json.loads(t_row["data"])
            tdata["bed_held"] = False
            tdata["received_at"] = utcnow()
            self._write_transfer(connection, transfer_id, "received", tdata)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(transfer_id)

    def void_transfer_atomic(self, transfer_id, medical_point_id):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            t_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (transfer_id,)
            ).fetchone()
            if not t_row:
                raise NotFoundError("transfer not found: " + transfer_id)
            tdata = json.loads(t_row["data"])
            if tdata.get("bed_held"):
                mp_row = connection.execute(
                    "SELECT * FROM entities WHERE id = ?", (medical_point_id,)
                ).fetchone()
                if mp_row:
                    _, _, reserved = self._load_medical_point(mp_row)
                    mp_data = json.loads(mp_row["data"])
                    mp_data["reserved"] = max(0, reserved - 1)
                    self._write_medical_point(connection, medical_point_id, mp_data)
            tdata["bed_held"] = False
            tdata["voided_at"] = utcnow()
            self._write_transfer(connection, transfer_id, "void", tdata)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(transfer_id)

    def transition_medical_point(self, medical_point_id, expected_version, next_status, new_data, void_transfers=False):
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            mp_row = connection.execute(
                "SELECT * FROM entities WHERE id = ?", (medical_point_id,)
            ).fetchone()
            if not mp_row:
                raise NotFoundError("medical point not found: " + medical_point_id)
            if expected_version is not None and int(mp_row["version"]) != int(expected_version):
                raise ConflictError(
                    "version conflict: expected %s, found %s"
                    % (expected_version, mp_row["version"])
                )
            now = utcnow()
            connection.execute(
                "UPDATE entities SET status = ?, version = version + 1, data = ?, updated_at = ? WHERE id = ?",
                (next_status, json.dumps(new_data, ensure_ascii=False, sort_keys=True), now, medical_point_id),
            )
            if void_transfers:
                _, _, reserved = self._load_medical_point(mp_row)
                rows = connection.execute(
                    "SELECT id, data FROM entities WHERE kind = 'transfer' "
                    "AND status IN ('reserved', 'dispatched')",
                ).fetchall()
                released = 0
                for row in rows:
                    tdata = json.loads(row["data"])
                    if tdata.get("medical_point_id") != medical_point_id:
                        continue
                    if tdata.get("bed_held"):
                        released += 1
                        tdata["bed_held"] = False
                    tdata["voided_at"] = now
                    self._write_transfer(connection, row["id"], "void", tdata)
                if released:
                    updated = dict(new_data)
                    updated["reserved"] = max(0, reserved - released)
                    self._write_medical_point(connection, medical_point_id, updated)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self.get_entity(medical_point_id)
