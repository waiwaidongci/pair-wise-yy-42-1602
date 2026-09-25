import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied
from src.repository import Repository
from src.service import Service


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "fire", "description": "dispatch", "severity": "high",
             "quantity": 5, "threshold": 10}, "creator", "field_commander")
        self.zone = self.service.create_zone(
            self.item["id"], {"code": "Z-A", "name": "东区"}, "fc", "field_commander")
        self.other = self.service.create_zone(
            self.item["id"], {"code": "Z-B", "name": "西区"}, "fc", "field_commander")
        self.team = self.service.create_resource(
            {"code": "T-1", "name": "一队", "kind": "team"}, "lg", "logistics")
        self.truck = self.service.create_resource(
            {"code": "V-1", "name": "水车", "kind": "vehicle"}, "lg", "logistics")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_overlapping_assignment_blocked_with_conflict_zone(self):
        self.service.assign(
            {"zone_id": self.zone["id"], "resource_id": self.team["id"],
             "arrived_at": "2026-09-25T08:00:00Z",
             "withdrawn_at": "2026-09-25T12:00:00Z"}, "lg", "logistics")
        with self.assertRaises(ConflictError) as ctx:
            self.service.assign(
                {"zone_id": self.other["id"], "resource_id": self.team["id"],
                 "arrived_at": "2026-09-25T11:00:00Z",
                 "withdrawn_at": "2026-09-25T13:00:00Z"}, "lg", "logistics")
        conflict = ctx.exception.details["conflicts"][0]
        self.assertEqual(conflict["zone_id"], self.zone["id"])
        self.assertEqual(conflict["zone_code"], "Z-A")

    def test_open_ended_occupancy_blocks_until_withdrawn(self):
        occupancy = self.service.assign(
            {"zone_id": self.zone["id"], "resource_id": self.team["id"],
             "arrived_at": "2026-09-25T08:00:00Z"}, "lg", "logistics")
        with self.assertRaises(ConflictError):
            self.service.assign(
                {"zone_id": self.other["id"], "resource_id": self.team["id"],
                 "arrived_at": "2026-09-26T08:00:00Z"}, "lg", "logistics")
        self.service.withdraw(
            occupancy["id"], {"withdrawn_at": "2026-09-25T18:00:00Z"},
            "lg", "logistics")
        new = self.service.assign(
            {"zone_id": self.other["id"], "resource_id": self.team["id"],
             "arrived_at": "2026-09-25T18:00:00Z"}, "lg", "logistics")
        self.assertEqual(new["zone_id"], self.other["id"])

    def test_withdrawal_before_arrival_rejected(self):
        occupancy = self.service.assign(
            {"zone_id": self.zone["id"], "resource_id": self.team["id"],
             "arrived_at": "2026-09-25T10:00:00Z"}, "lg", "logistics")
        with self.assertRaises(ConflictError):
            self.service.withdraw(
                occupancy["id"], {"withdrawn_at": "2026-09-25T09:00:00Z"},
                "lg", "logistics")

    def test_close_zone_blocked_then_allowed(self):
        occupancy = self.service.assign(
            {"zone_id": self.zone["id"], "resource_id": self.team["id"],
             "arrived_at": "2026-09-25T08:00:00Z"}, "lg", "logistics")
        with self.assertRaises(ConflictError):
            self.service.close_zone(self.zone["id"], 1, "ic", "incident_commander")
        self.service.withdraw(
            occupancy["id"], {"withdrawn_at": "2026-09-25T15:00:00Z"},
            "lg", "logistics")
        closed = self.service.close_zone(self.zone["id"], 1, "ic", "incident_commander")
        self.assertEqual(closed["status"], "closed")
        with self.assertRaises(ConflictError):
            self.service.assign(
                {"zone_id": self.zone["id"], "resource_id": self.truck["id"],
                 "arrived_at": "2026-09-25T16:00:00Z"}, "lg", "logistics")

    def test_roles_for_dispatch(self):
        payload = {"zone_id": self.zone["id"], "resource_id": self.team["id"],
                   "arrived_at": "2026-09-25T08:00:00Z"}
        with self.assertRaises(PermissionDenied):
            self.service.assign(payload, "x", "viewer")
        with self.assertRaises(PermissionDenied):
            self.service.create_resource(
                {"code": "T-9", "name": "x", "kind": "team"}, "x", "viewer")


if __name__ == "__main__":
    unittest.main()
