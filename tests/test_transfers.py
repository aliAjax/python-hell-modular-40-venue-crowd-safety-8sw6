import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import (
    Actor,
    ConflictError,
    InvalidTransition,
    PermissionDenied,
)
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TransferWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "transfer.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("commander-1", "coordinator")
        self.coordinator2 = Actor("commander-2", "coordinator")
        self.supervisor = Actor("supervisor", "supervisor")
        self.operator = Actor("field-operator", "operator")
        self.handover = Actor("night-commander", "coordinator")
        self._build_venue()

    def tearDown(self):
        self.tmp.cleanup()

    def _build_venue(self, point_capacity=2):
        service = self.service
        self.venue = service.create(
            self.coordinator, "venue", {"name": "Arena", "address": "1 Road"}
        )
        self.zone = service.create(
            self.coordinator,
            "zone",
            {"venue_id": self.venue["id"], "name": "Floor", "capacity": 500},
        )
        self.point = service.create(
            self.supervisor,
            "medical_point",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "capacity": point_capacity,
                "equipment_level": "advanced",
            },
        )
        self.point = service.transition(
            self.supervisor, self.point["id"], "activate", {}
        )
        self.incident = service.create(
            self.operator,
            "incident",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "source_ref": "radio-med-1",
                "incident_type": "medical",
                "severity": "high",
                "reported_at": "2026-10-04T10:00:00Z",
            },
        )
        self.incident = service.transition(
            self.supervisor, self.incident["id"], "triage", {"priority": "medical"}
        )

    def _second_incident(self, source_ref="radio-med-2"):
        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "source_ref": source_ref,
                "incident_type": "medical",
                "severity": "medium",
                "reported_at": "2026-10-04T10:05:00Z",
            },
        )
        return self.service.transition(
            self.supervisor, incident["id"], "triage", {"priority": "medical"}
        )

    def test_reserve_holds_a_bed_without_admitting(self):
        transfer = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
        )
        self.assertEqual(transfer["status"], "reserved")
        point = self.service.get(self.point["id"])
        self.assertEqual(point["data"]["beds_held"], 1)
        self.assertEqual(point["data"]["patients"], 0)

    def test_incident_keeps_a_single_transfer_order(self):
        self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator2,
                "transfer",
                {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
            )

    def test_concurrent_reserve_first_wins_loser_sees_occupied(self):
        # capacity is 2: four commanders race for beds, two must lose
        incident2 = self._second_incident("radio-med-c2")
        incident3 = self._second_incident("radio-med-c3")
        requests = [
            (self.coordinator, self.incident),
            (self.coordinator2, incident2),
            (Actor("commander-3", "coordinator"), incident3),
            # same incident as request 0: only one order may ever exist
            (Actor("commander-4", "coordinator"), self.incident),
        ]
        start = threading.Barrier(len(requests))
        results = {"ok": [], "errors": []}
        lock = threading.Lock()

        def reserve(actor, incident):
            try:
                start.wait(timeout=10)
                transfer = self.service.create(
                    actor,
                    "transfer",
                    {"incident_id": incident["id"], "medical_point_id": self.point["id"]},
                )
                with lock:
                    results["ok"].append(transfer["id"])
            except ConflictError as exc:
                with lock:
                    results["errors"].append(str(exc))
            except Exception as exc:  # noqa: BLE001 - surfaced to the test thread
                with lock:
                    results["errors"].append("UNEXPECTED: %s %s" % (type(exc).__name__, exc))

        threads = [threading.Thread(target=reserve, args=args) for args in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(results["ok"]), 2, results)
        self.assertEqual(len(results["errors"]), 2, results)
        point = self.service.get(self.point["id"])
        self.assertEqual(point["data"]["beds_held"], 2)
        self.assertEqual(point["data"]["patients"], 0)
        transfers = self.service.list("transfer")
        self.assertEqual(len(transfers), 2)
        self.assertTrue(all(t["status"] == "reserved" for t in transfers))

    def test_dispatch_failure_returns_bed_and_retry_reuses_order(self):
        transfer = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
        )
        # medic-A already holds an active task from an unrelated incident
        other = self._second_incident("radio-med-3")
        other_transfer = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": other["id"], "medical_point_id": self.point["id"]},
        )
        self.service.transition(
            self.coordinator,
            other_transfer["id"],
            "dispatch",
            {"team_id": "medic-a"},
        )

        point = self.service.get(self.point["id"])
        self.assertEqual(point["data"]["beds_held"], 2)

        with self.assertRaises(ConflictError):
            self.service.transition(
                self.coordinator,
                transfer["id"],
                "dispatch",
                {"team_id": "medic-a"},
            )

        transfer = self.service.get(transfer["id"])
        point = self.service.get(self.point["id"])
        self.assertEqual(transfer["status"], "released")
        self.assertEqual(point["data"]["beds_held"], 1)
        self.assertEqual(point["data"]["patients"], 0)

        # retry: same order, bed re-acquired exactly once, no duplicate task
        transfer = self.service.transition(
            self.coordinator,
            transfer["id"],
            "dispatch",
            {"team_id": "medic-b"},
        )
        self.assertEqual(transfer["status"], "dispatched")
        self.assertEqual(transfer["data"]["attempts"], 1)
        point = self.service.get(self.point["id"])
        self.assertEqual(point["data"]["beds_held"], 2)
        tasks = self.service.list("task")
        self.assertEqual(len(tasks), 2)
        own_tasks = [t for t in tasks if t["data"].get("transfer_id") == transfer["id"]]
        self.assertEqual(len(own_tasks), 1)
        self.assertEqual(own_tasks[0]["status"], "assigned")
        # incident is linked and advanced as part of the same unit of work
        self.assertEqual(self.service.get(self.incident["id"])["status"], "dispatched")

    def test_handover_receive_increments_bed_usage_and_admissions(self):
        transfer = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
        )
        transfer = self.service.transition(
            self.coordinator, transfer["id"], "dispatch", {"team_id": "medic-a"}
        )
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                Actor("viewer", "viewer"),
                transfer["id"],
                "receive",
                {"receiver_id": "night-commander", "received_at": "2026-10-04T10:30:00Z"},
            )
        transfer = self.service.transition(
            self.handover,
            transfer["id"],
            "receive",
            {"receiver_id": "night-commander", "received_at": "2026-10-04T10:30:00Z"},
        )
        self.assertEqual(transfer["status"], "received")
        point = self.service.get(self.point["id"])
        self.assertEqual(point["data"]["beds_held"], 0)
        self.assertEqual(point["data"]["patients"], 1)

        # a received order cannot be dispatched or received again
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, transfer["id"], "dispatch", {"team_id": "medic-b"}
            )

    def test_point_change_voids_open_orders_admissions_are_kept(self):
        accepted = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
        )
        self.service.transition(
            self.coordinator, accepted["id"], "dispatch", {"team_id": "medic-a"}
        )
        self.service.transition(
            self.handover,
            accepted["id"],
            "receive",
            {"receiver_id": "night-commander", "received_at": "2026-10-04T10:20:00Z"},
        )
        waiting_incident = self._second_incident("radio-med-4")
        waiting = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": waiting_incident["id"], "medical_point_id": self.point["id"]},
        )
        # capacity is 2: one patient admitted, one bed held
        point = self.service.get(self.point["id"])
        self.assertEqual((point["data"]["patients"], point["data"]["beds_held"]), (1, 1))

        self.service.transition(
            self.supervisor, self.point["id"], "mark_full", {"reason": "surge"}
        )

        self.assertEqual(self.service.get(waiting["id"])["status"], "void")
        self.assertEqual(self.service.get(accepted["id"])["status"], "received")
        point = self.service.get(self.point["id"])
        self.assertEqual(point["status"], "full")
        # held beds returned, admissions untouched
        self.assertEqual(point["data"]["beds_held"], 0)
        self.assertEqual(point["data"]["patients"], 1)

        # commander opens another medical point and reselects on the SAME order
        waiting_before = waiting
        point2 = self.service.create(
            self.supervisor,
            "medical_point",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "capacity": 4,
                "equipment_level": "standard",
            },
        )
        self.service.transition(self.supervisor, point2["id"], "activate", {})
        waiting = self.service.transition(
            self.coordinator, waiting["id"], "reselect", {"medical_point_id": point2["id"]}
        )
        self.assertEqual(waiting["status"], "reserved")
        self.assertEqual(waiting["id"], waiting_before["id"])  # reselect keeps the same order
        self.assertEqual(waiting["data"]["medical_point_id"], point2["id"])
        self.assertEqual(
            waiting["data"]["medical_point_history"][0]["medical_point_id"],
            self.point["id"],
        )
        point2 = self.service.get(point2["id"])
        self.assertEqual(point2["data"]["beds_held"], 1)

        # the original full point still shows the admitted patient
        self.assertEqual(self.service.get(self.point["id"])["data"]["patients"], 1)
        self.assertEqual(len(self.service.list("transfer")), 2)

    def test_cannot_reserve_before_triage_or_into_inactive_point(self):
        fresh = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "source_ref": "radio-med-9",
                "incident_type": "medical",
                "severity": "low",
                "reported_at": "2026-10-04T11:00:00Z",
            },
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "transfer",
                {"incident_id": fresh["id"], "medical_point_id": self.point["id"]},
            )
        standby = self.service.create(
            self.supervisor,
            "medical_point",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "capacity": 3,
                "equipment_level": "basic",
            },
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator,
                "transfer",
                {"incident_id": self.incident["id"], "medical_point_id": standby["id"]},
            )

    def test_reselect_only_allowed_on_void_order(self):
        transfer = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
        )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.coordinator, transfer["id"], "reselect",
                {"medical_point_id": self.point["id"]},
            )

    def test_viewer_cannot_run_transfer_actions(self):
        viewer = Actor("viewer", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create(
                viewer,
                "transfer",
                {"incident_id": self.incident["id"], "medical_point_id": self.point["id"]},
            )

    def test_optimistic_version_guards_reselect(self):
        waiting_incident = self._second_incident("radio-med-5")
        transfer = self.service.create(
            self.coordinator,
            "transfer",
            {"incident_id": waiting_incident["id"], "medical_point_id": self.point["id"]},
        )
        self.service.transition(
            self.supervisor, self.point["id"], "mark_full", {"reason": "surge"}
        )
        point2 = self.service.create(
            self.supervisor,
            "medical_point",
            {
                "venue_id": self.venue["id"],
                "zone_id": self.zone["id"],
                "capacity": 4,
                "equipment_level": "standard",
            },
        )
        self.service.transition(self.supervisor, point2["id"], "activate", {})
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.coordinator,
                transfer["id"],
                "reselect",
                {"medical_point_id": point2["id"]},
                expected_version=transfer["version"],
            )


if __name__ == "__main__":
    unittest.main()
