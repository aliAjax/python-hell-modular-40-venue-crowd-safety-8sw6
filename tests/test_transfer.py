import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import Actor, ConflictError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class TransferTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.service = DomainService(
            SQLiteRepository(Path(self.tmp.name) / "transfer.db"),
            RuleEngine(),
        )
        self.coordinator = Actor("venue-commander", "coordinator")
        self.supervisor = Actor("safety-supervisor", "supervisor")
        self.operator = Actor("operator", "operator")

    def tearDown(self):
        self.tmp.cleanup()

    def _venue_zone(self, capacity=100):
        venue = self.service.create(
            self.coordinator, "venue", {"name": "V", "address": "A"}
        )
        zone = self.service.create(
            self.coordinator, "zone",
            {"venue_id": venue["id"], "name": "Z", "capacity": capacity},
        )
        return venue, zone

    def _medical_point(self, venue, zone, capacity, activate=True):
        mp = self.service.create(
            self.supervisor,
            "medical_point",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "capacity": capacity,
                "equipment_level": "advanced",
            },
        )
        if activate:
            mp = self.service.transition(self.supervisor, mp["id"], "activate", {})
        return mp

    def _incident(self, venue, zone, ref, triage=True):
        incident = self.service.create(
            self.operator,
            "incident",
            {
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "source_ref": ref,
                "incident_type": "medical",
                "severity": "high",
                "reported_at": "t",
            },
        )
        if triage:
            incident = self.service.transition(
                self.supervisor, incident["id"], "triage", {"priority": "medical"}
            )
        return incident

    def _busy_team(self, venue, zone, team_id):
        incident = self._incident(venue, zone, "busy-" + team_id)
        task = self.service.create(
            self.supervisor,
            "task",
            {
                "incident_id": incident["id"],
                "venue_id": venue["id"],
                "zone_id": zone["id"],
                "team_id": team_id,
                "task_type": "medical",
            },
        )
        self.service.transition(
            self.coordinator, task["id"], "assign", {"assigned_at": "t"}
        )
        return task

    def test_reserve_bed_holds_it(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=2)
        incident = self._incident(venue, zone, "r1")
        transfer = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": incident["id"], "medical_point_id": mp["id"]},
        )
        self.assertEqual(transfer["status"], "reserved")
        self.assertTrue(transfer["data"]["bed_held"])
        mp = self.service.get(mp["id"])
        self.assertEqual(mp["data"]["reserved"], 1)
        self.assertEqual(mp["data"]["patients"], 0)

    def test_one_transfer_per_incident(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=2)
        incident = self._incident(venue, zone, "r1")
        self.service.create(
            self.coordinator, "transfer",
            {"incident_id": incident["id"], "medical_point_id": mp["id"]},
        )
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator, "transfer",
                {"incident_id": incident["id"], "medical_point_id": mp["id"]},
            )

    def test_incident_must_be_triaged(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=2)
        incident = self._incident(venue, zone, "r1", triage=False)
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator, "transfer",
                {"incident_id": incident["id"], "medical_point_id": mp["id"]},
            )

    def test_medical_point_must_be_active(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=2, activate=False)
        incident = self._incident(venue, zone, "r1")
        with self.assertRaises(ConflictError):
            self.service.create(
                self.coordinator, "transfer",
                {"incident_id": incident["id"], "medical_point_id": mp["id"]},
            )

    def test_concurrent_reserve_one_bed(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=1)
        inc_a = self._incident(venue, zone, "A")
        inc_b = self._incident(venue, zone, "B")
        results = {}

        def reserve(name, incident):
            try:
                transfer = self.service.create(
                    self.coordinator, "transfer",
                    {"incident_id": incident["id"], "medical_point_id": mp["id"]},
                )
                results[name] = ("ok", transfer["id"])
            except ConflictError as exc:
                results[name] = ("fail", str(exc))

        first = threading.Thread(target=reserve, args=("first", inc_a))
        second = threading.Thread(target=reserve, args=("second", inc_b))
        first.start()
        second.start()
        first.join()
        second.join()
        outcomes = [results["first"][0], results["second"][0]]
        self.assertEqual(outcomes.count("ok"), 1)
        self.assertEqual(outcomes.count("fail"), 1)
        failed = results["first"] if results["first"][0] == "fail" else results["second"]
        self.assertIn("bed occupied", failed[1])
        mp = self.service.get(mp["id"])
        self.assertEqual(mp["data"]["reserved"], 1)

    def test_dispatch_failure_returns_bed_and_retry(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=1)
        incident = self._incident(venue, zone, "r1")
        transfer = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": incident["id"], "medical_point_id": mp["id"]},
        )
        self._busy_team(venue, zone, "team-busy")
        # First dispatch fails: the treatment group is still occupied.
        with self.assertRaises(ConflictError):
            self.service.transition(
                self.coordinator, transfer["id"], "dispatch",
                {"team_id": "team-busy", "task_type": "medical", "assigned_at": "t"},
            )
        mp = self.service.get(mp["id"])
        self.assertEqual(mp["data"]["reserved"], 0)
        transfer = self.service.get(transfer["id"])
        self.assertFalse(transfer["data"]["bed_held"])
        # Retry uses the same transfer form; bed is re-occupied, not doubled.
        transfer = self.service.transition(
            self.coordinator, transfer["id"], "dispatch",
            {"team_id": "team-free", "task_type": "medical", "assigned_at": "t"},
        )
        self.assertEqual(transfer["status"], "dispatched")
        self.assertTrue(transfer["data"]["bed_held"])
        mp = self.service.get(mp["id"])
        self.assertEqual(mp["data"]["reserved"], 1)
        # Idempotent retry returns the same dispatched transfer.
        again = self.service.transition(
            self.coordinator, transfer["id"], "dispatch",
            {"team_id": "team-free", "task_type": "medical", "assigned_at": "t"},
        )
        self.assertEqual(again["id"], transfer["id"])
        self.assertEqual(again["status"], "dispatched")

    def test_medical_point_status_change_voids_transfers(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=2)
        inc_a = self._incident(venue, zone, "A")
        inc_b = self._incident(venue, zone, "B")
        t_a = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": inc_a["id"], "medical_point_id": mp["id"]},
        )
        t_b = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": inc_b["id"], "medical_point_id": mp["id"]},
        )
        self.service.transition(
            self.coordinator, t_a["id"], "dispatch",
            {"team_id": "team-a", "task_type": "medical", "assigned_at": "t"},
        )
        mp = self.service.transition(
            self.supervisor, mp["id"], "mark_full", {"reason": "full"}
        )
        self.assertEqual(mp["status"], "full")
        self.assertEqual(mp["data"]["reserved"], 0)
        self.assertEqual(mp["data"]["patients"], 0)
        self.assertEqual(self.service.get(t_a["id"])["status"], "void")
        self.assertEqual(self.service.get(t_b["id"])["status"], "void")

    def test_reselect_after_void(self):
        venue, zone = self._venue_zone()
        mp_full = self._medical_point(venue, zone, capacity=1)
        mp_open = self._medical_point(venue, zone, capacity=2)
        incident = self._incident(venue, zone, "r1")
        transfer = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": incident["id"], "medical_point_id": mp_full["id"]},
        )
        self.service.transition(
            self.supervisor, mp_full["id"], "mark_full", {"reason": "full"}
        )
        self.assertEqual(self.service.get(transfer["id"])["status"], "void")
        # Commander reselects another medical point; a new transfer is allowed.
        new_transfer = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": incident["id"], "medical_point_id": mp_open["id"]},
        )
        self.assertEqual(new_transfer["status"], "reserved")
        self.assertEqual(new_transfer["data"]["medical_point_id"], mp_open["id"])
        mp_open = self.service.get(mp_open["id"])
        self.assertEqual(mp_open["data"]["reserved"], 1)

    def test_receive_admits_patient(self):
        venue, zone = self._venue_zone()
        mp = self._medical_point(venue, zone, capacity=1)
        incident = self._incident(venue, zone, "r1")
        transfer = self.service.create(
            self.coordinator, "transfer",
            {"incident_id": incident["id"], "medical_point_id": mp["id"]},
        )
        self.service.transition(
            self.coordinator, transfer["id"], "dispatch",
            {"team_id": "team-a", "task_type": "medical", "assigned_at": "t"},
        )
        transfer = self.service.transition(
            self.coordinator, transfer["id"], "receive", {}
        )
        self.assertEqual(transfer["status"], "received")
        self.assertFalse(transfer["data"]["bed_held"])
        mp = self.service.get(mp["id"])
        self.assertEqual(mp["data"]["patients"], 1)
        self.assertEqual(mp["data"]["reserved"], 0)


if __name__ == "__main__":
    unittest.main()
