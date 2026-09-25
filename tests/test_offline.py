import tempfile, unittest
from pathlib import Path
from datetime import datetime, timezone
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import evaluate_phase
from src.service import Service


def dt(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


class MergeRulesTest(unittest.TestCase):
    def test_earliest_field_time_wins(self):
        current = {"arrived": dt("2026-09-25T08:00:00Z")}
        self.assertEqual(evaluate_phase("arrived", dt("2026-09-25T09:00:00Z"), current), "duplicate")
        self.assertEqual(evaluate_phase("arrived", dt("2026-09-25T07:00:00Z"), current), "conflict")

    def test_phase_order_conflicts(self):
        ordered = {"departed": dt("2026-09-25T07:00:00Z"),
                   "arrived": dt("2026-09-25T08:00:00Z")}
        self.assertEqual(evaluate_phase("withdrawn", dt("2026-09-25T06:00:00Z"), ordered), "conflict")
        self.assertEqual(evaluate_phase("withdrawn", dt("2026-09-25T09:00:00Z"), ordered), "accept")
        # 同一阶段更晚的现场时间属于重复，最早时间胜出
        self.assertEqual(evaluate_phase("departed", dt("2026-09-25T07:30:00Z"), ordered), "duplicate")

    def test_unknown_phase(self):
        with self.assertRaises(ValidationError):
            evaluate_phase("nope", dt("2026-09-25T08:00:00Z"), {})


class OfflineBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "fire", "description": "offline", "severity": "high",
             "quantity": 5, "threshold": 10}, "creator", "field_commander")
        self.zone = self.service.create_zone(
            self.item["id"], {"code": "Z-A", "name": "东区"}, "fc", "field_commander")
        self.team = self.service.create_resource(
            {"code": "T-1", "name": "一队", "kind": "team"}, "lg", "logistics")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _upload(self, reports, batch_no="B-1"):
        return self.service.upload_offline(
            {"batch_no": batch_no, "zone_id": self.zone["id"],
             "resource_id": self.team["id"], "reports": reports},
            "fc", "field_commander")

    def test_repeated_upload_merges_and_flags_contradiction(self):
        batch = self._upload([
            {"status": "departed", "event_time": "2026-09-25T07:00:00Z"},
            {"status": "arrived", "event_time": "2026-09-25T08:00:00Z"},
        ])
        self.assertEqual(batch["phases"]["arrived"], "2026-09-25T08:00:00+00:00")
        self.assertEqual(batch["pending"], [])
        # 重复上传：更早到场为矛盾状态，更晚到场为重复
        batch = self._upload([
            {"status": "arrived", "event_time": "2026-09-25T06:30:00Z"},
            {"status": "arrived", "event_time": "2026-09-25T09:00:00Z"},
            {"status": "withdrawn", "event_time": "2026-09-25T18:00:00Z"},
        ])
        self.assertEqual(len(batch["pending"]), 1)
        self.assertEqual(batch["pending"][0]["phase"], "arrived")
        self.assertEqual(batch["phases"]["arrived"], "2026-09-25T08:00:00+00:00")
        self.assertEqual(batch["phases"]["withdrawn"], "2026-09-25T18:00:00+00:00")

    def test_accept_contradiction_applies_occupancy(self):
        self._upload([
            {"status": "arrived", "event_time": "2026-09-25T08:00:00Z"}])
        batch = self._upload([
            {"status": "arrived", "event_time": "2026-09-25T07:00:00Z"}])
        self.assertEqual(len(batch["pending"]), 1)
        # 未确认前不能关闭任务区
        with self.assertRaises(ConflictError):
            self.service.close_zone(self.zone["id"], 1, "ic", "incident_commander")
        resolved = self.service.confirm_offline(
            batch["id"], {"index": 0, "accepted": True}, "ic", "incident_commander")
        self.assertEqual(resolved["phases"]["arrived"], "2026-09-25T07:00:00+00:00")
        occupancies = self.service.list_occupancies("viewer", zone_id=self.zone["id"])
        self.assertEqual(len(occupancies), 1)
        self.assertEqual(occupancies[0]["arrived_at"], "2026-09-25T07:00:00+00:00")
        self.assertIsNone(occupancies[0]["withdrawn_at"])
        # 资源仍在场，关闭仍被挡
        with self.assertRaises(ConflictError):
            self.service.close_zone(self.zone["id"], 1, "ic", "incident_commander")

    def test_reject_contradiction_keeps_original(self):
        self._upload([
            {"status": "arrived", "event_time": "2026-09-25T08:00:00Z"}])
        batch = self._upload([
            {"status": "arrived", "event_time": "2026-09-25T07:00:00Z"}])
        resolved = self.service.confirm_offline(
            batch["id"], {"index": 0, "accepted": False}, "ic", "incident_commander")
        self.assertEqual(resolved["pending"], [])
        self.assertEqual(resolved["phases"]["arrived"], "2026-09-25T08:00:00+00:00")
        occupancies = self.service.list_occupancies("viewer", zone_id=self.zone["id"])
        self.assertEqual(occupancies[0]["arrived_at"], "2026-09-25T08:00:00+00:00")

    def test_offline_withdrawal_reconciles_occupancy(self):
        batch = self._upload([
            {"status": "departed", "event_time": "2026-09-25T06:00:00Z"},
            {"status": "arrived", "event_time": "2026-09-25T07:00:00Z"},
            {"status": "withdrawn", "event_time": "2026-09-25T17:00:00Z"},
        ])
        occupancies = self.service.list_occupancies("viewer", zone_id=self.zone["id"])
        self.assertEqual(len(occupancies), 1)
        self.assertEqual(occupancies[0]["withdrawn_at"], "2026-09-25T17:00:00+00:00")
        closed = self.service.close_zone(self.zone["id"], 1, "ic", "incident_commander")
        self.assertEqual(closed["status"], "closed")
        self.assertTrue(self.repo.verify_audit_chain())

    def test_batch_no_rebound_to_other_target_conflicts(self):
        self._upload([{"status": "arrived", "event_time": "2026-09-25T08:00:00Z"}])
        other = self.service.create_resource(
            {"code": "T-2", "name": "二队", "kind": "team"}, "lg", "logistics")
        with self.assertRaises(ConflictError):
            self.service.upload_offline(
                {"batch_no": "B-1", "zone_id": self.zone["id"],
                 "resource_id": other["id"],
                 "reports": [{"status": "arrived", "event_time": "2026-09-25T09:00:00Z"}]},
                "fc", "field_commander")

    def test_viewer_cannot_upload(self):
        with self.assertRaises(PermissionDenied):
            self.service.upload_offline(
                {"batch_no": "B-9", "zone_id": self.zone["id"],
                 "resource_id": self.team["id"],
                 "reports": [{"status": "arrived", "event_time": "2026-09-25T08:00:00Z"}]},
                "x", "viewer")


if __name__ == "__main__":
    unittest.main()
