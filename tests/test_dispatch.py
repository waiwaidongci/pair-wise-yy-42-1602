import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class DispatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.zone_a = self.service.create_zone(
            {"name": "A线", "description": "东线"}, "ic", "incident_commander")
        self.zone_b = self.service.create_zone(
            {"name": "B线", "description": "西线"}, "ic", "incident_commander")
        self.team = self.service.create_resource(
            {"code": "T-01", "kind": "crew", "name": "一队"}, "ic", "incident_commander")
        self.truck = self.service.create_resource(
            {"code": "V-09", "kind": "truck", "name": "水罐车9号"}, "ic", "incident_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def dispatch(self, zone, resource, start, end=None, role="incident_commander"):
        payload = {"resource_id": resource["id"], "zone_id": zone["id"], "start_at": start}
        if end:
            payload["end_at"] = end
        return self.service.dispatch(payload, "dispatcher", role)

    def test_overlapping_dispatch_blocked_and_names_conflict_zone(self):
        # 一队10:00到场A线，未撤离（开放区间）
        self.dispatch(self.zone_a, self.team, "2026-09-25T10:00:00+00:00")
        # 11:00又被派往B线：必须挡住，并指出冲突任务区是A线
        with self.assertRaises(ConflictError) as ctx:
            self.dispatch(self.zone_b, self.team, "2026-09-25T11:00:00+00:00")
        details = ctx.exception.details
        self.assertIsNotNone(details)
        self.assertEqual([c["zone_id"] for c in details["conflicts"]], [self.zone_a["id"]])
        self.assertEqual(details["conflicts"][0]["zone_name"], "A线")
        # 分配确实没落库
        self.assertEqual(
            len(self.service.list_zone_assignments(self.zone_b["id"], "viewer")), 0)

    def test_back_to_back_shifts_do_not_overlap(self):
        # 班次交接：10:00-12:00在A线，12:00整去B线，端点相接不算冲突
        self.dispatch(self.zone_a, self.team, "2026-09-25T10:00:00+00:00",
                      "2026-09-25T12:00:00+00:00")
        assignment = self.dispatch(self.zone_b, self.team, "2026-09-25T12:00:00+00:00")
        self.assertEqual(assignment["zone_id"], self.zone_b["id"])
        # 哪怕错开一分钟也要挡
        with self.assertRaises(ConflictError):
            self.dispatch(self.zone_a, self.team, "2026-09-25T11:59:00+00:00",
                          "2026-09-25T12:30:00+00:00")

    def test_release_frees_resource_for_other_zone(self):
        self.dispatch(self.zone_a, self.team, "2026-09-25T10:00:00+00:00")
        with self.assertRaises(ConflictError):
            self.dispatch(self.zone_b, self.team, "2026-09-25T13:00:00+00:00")
        self.service.release(
            {"resource_id": self.team["id"], "zone_id": self.zone_a["id"],
             "end_at": "2026-09-25T12:00:00+00:00"}, "dispatcher", "incident_commander")
        # 撤离后可派往B线
        self.dispatch(self.zone_b, self.team, "2026-09-25T12:30:00+00:00")

    def test_resources_are_independent(self):
        self.dispatch(self.zone_a, self.team, "2026-09-25T10:00:00+00:00")
        # 水罐车没被占用，同一时段同任务区可派
        self.dispatch(self.zone_a, self.truck, "2026-09-25T10:00:00+00:00")

    def test_dispatch_permission_and_closed_zone(self):
        with self.assertRaises(PermissionDenied):
            self.dispatch(self.zone_a, self.team, "2026-09-25T10:00:00+00:00", role="viewer")
        self.service.close_zone(self.zone_b["id"], {"expected_version": 1},
                                "ic", "incident_commander")
        with self.assertRaises(ConflictError):
            self.dispatch(self.zone_b, self.team, "2026-09-25T10:00:00+00:00")

    def test_offline_arrival_blocks_later_dispatch(self):
        result = self.service.upload_offline_batch(
            {"batch_no": "B-100", "events": [
                {"event": "departed", "resource_id": self.team["id"],
                 "zone_id": self.zone_a["id"], "field_time": "2026-09-25T09:00:00+00:00"},
                {"event": "arrived", "resource_id": self.team["id"],
                 "zone_id": self.zone_a["id"], "field_time": "2026-09-25T10:00:00+00:00"},
            ]}, "field", "field_commander")
        self.assertFalse(result["has_contradiction"])
        with self.assertRaises(ConflictError) as ctx:
            self.dispatch(self.zone_b, self.team, "2026-09-25T11:00:00+00:00")
        self.assertEqual(ctx.exception.details["conflicts"][0]["zone_id"], self.zone_a["id"])


if __name__ == "__main__":
    unittest.main()
