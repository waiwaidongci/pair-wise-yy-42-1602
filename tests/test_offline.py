import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service


class OfflineBatchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.zone = self.service.create_zone(
            {"name": "C线", "description": "北线"}, "ic", "incident_commander")
        self.team = self.service.create_resource(
            {"code": "T-22", "kind": "crew", "name": "二十二队"}, "ic", "incident_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def event(self, kind, when):
        return {"event": kind, "resource_id": self.team["id"],
                "zone_id": self.zone["id"], "field_time": when}

    def upload(self, events, actor="field", role="field_commander", batch="BATCH-1"):
        return self.service.upload_offline_batch(
            {"batch_no": batch, "events": events}, actor, role)

    def test_duplicate_upload_merges_earliest_field_time(self):
        # 第一次上传：到场11:00；信号恢复后重复上传同批次，到场写成10:30（更早的现场时间）
        self.upload([self.event("arrived", "2026-09-25T11:00:00+00:00")])
        result = self.upload([self.event("arrived", "2026-09-25T10:30:00+00:00")])
        report = [r for r in result["reports"] if r["event"] == "arrived"][0]
        self.assertEqual(report["field_time"], "2026-09-25T10:30:00+00:00")
        self.assertEqual(report["upload_count"], 2)
        self.assertEqual(len(result["reports"]), 1)  # 合并，不新增记录
        # 后到的更晚时间不会覆盖最早时间
        result = self.upload([self.event("arrived", "2026-09-25T12:00:00+00:00")])
        report = [r for r in result["reports"] if r["event"] == "arrived"][0]
        self.assertEqual(report["field_time"], "2026-09-25T10:30:00+00:00")
        self.assertEqual(report["upload_count"], 3)

    def test_contradiction_marked_pending_and_blocks_dispatch(self):
        # 出发10:00、到场09:00：到场早于出发，矛盾；后到的撤离标为待确认
        result = self.upload([
            self.event("departed", "2026-09-25T10:00:00+00:00"),
            self.event("arrived", "2026-09-25T09:00:00+00:00"),
        ])
        self.assertTrue(result["has_contradiction"])
        pending = [r for r in result["reports"] if r["review_status"] == "pending"]
        self.assertEqual([r["event"] for r in pending], ["arrived"])
        self.assertIn("矛盾", pending[0]["conflict_note"])
        # 撤离早于到场同样矛盾
        result = self.upload([self.event("evacuated", "2026-09-25T08:00:00+00:00")])
        statuses = {r["event"]: r["review_status"] for r in result["reports"]}
        self.assertEqual(statuses["evacuated"], "pending")
        # 待确认记录未决，任务区不能关闭
        zone = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone["pending_reports"], 2)
        with self.assertRaises(ConflictError) as ctx:
            self.service.close_zone(zone["id"], {"expected_version": zone["version"]},
                                    "ic", "incident_commander")
        self.assertIn("待确认", "；".join(ctx.exception.details["blockers"]))

    def test_dispatcher_confirms_then_occupancy_applies(self):
        self.upload([
            self.event("departed", "2026-09-25T10:00:00+00:00"),
            self.event("arrived", "2026-09-25T09:00:00+00:00"),
        ])
        # 矛盾未确认前，不产生现场占用
        zone_view = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone_view["open_assignments"], 0)
        pending = self.repo.list_reports("pending")[0]
        # viewer无权确认
        with self.assertRaises(PermissionDenied):
            self.service.review_offline_report(
                pending["id"], {"decision": "confirm"}, "ic", "viewer")
        confirmed = self.service.review_offline_report(
            pending["id"], {"decision": "confirm"}, "ic", "incident_commander")
        self.assertEqual(confirmed["review_status"], "confirmed")
        zone_view = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone_view["open_assignments"], 1)
        self.assertEqual(zone_view["pending_reports"], 0)

    def test_rejected_report_stays_rejected_on_reupload(self):
        self.upload([self.event("arrived", "2026-09-25T09:00:00+00:00"),
                     self.event("evacuated", "2026-09-25T08:00:00+00:00")])
        pending = [r for r in self.repo.list_reports("pending") if r["event"] == "evacuated"][0]
        self.service.review_offline_report(
            pending["id"], {"decision": "reject", "note": "撤离时间不可信"},
            "ic", "incident_commander")
        # 再次上传同批次撤离：不能把驳回记录悄悄改回确认/待确认
        result = self.upload([self.event("evacuated", "2026-09-25T15:00:00+00:00")])
        evacuated = [r for r in result["reports"] if r["event"] == "evacuated"][0]
        self.assertEqual(evacuated["review_status"], "rejected")
        # 资源仍未撤离（到场开放区间），任务区关闭继续被挡
        zone = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone["open_assignments"], 1)

    def test_batch_integrity_and_validation(self):
        # 同一批次号不能改挂到别的资源/任务区
        self.upload([self.event("arrived", "2026-09-25T10:00:00+00:00")])
        other_zone = self.service.create_zone(
            {"name": "D线"}, "ic", "incident_commander")
        with self.assertRaises(ConflictError):
            self.upload([{"event": "arrived", "resource_id": self.team["id"],
                          "zone_id": other_zone["id"],
                          "field_time": "2026-09-25T10:00:00+00:00"}])
        # 未知事件/坏时间
        with self.assertRaises(ValidationError):
            self.upload([self.event("flew", "2026-09-25T10:00:00+00:00")], batch="BATCH-2")
        with self.assertRaises(ValidationError):
            self.upload([self.event("arrived", "not-a-time")], batch="BATCH-3")
        # viewer不能回传
        with self.assertRaises(PermissionDenied):
            self.upload([self.event("arrived", "2026-09-25T10:00:00+00:00")],
                        role="viewer", batch="BATCH-4")

    def test_offline_evacuation_closes_dispatch_assignment(self):
        # 调度台先派单（开放占用），离线队伍回传同班次撤离：应闭合派单占用
        self.service.dispatch(
            {"resource_id": self.team["id"], "zone_id": self.zone["id"],
             "start_at": "2026-09-25T10:00:00+00:00"}, "dispatcher", "incident_commander")
        self.upload([self.event("evacuated", "2026-09-25T18:00:00+00:00")],
                    batch="BATCH-9")  # 只有撤离 -> pending（缺到场支撑）
        self.assertEqual(self.service.get_zone(self.zone["id"], "viewer")["pending_reports"], 1)
        # 补齐到场后撤离成立，占用闭合
        self.upload([
            self.event("departed", "2026-09-25T09:00:00+00:00"),
            self.event("arrived", "2026-09-25T10:00:00+00:00"),
        ], batch="BATCH-9")
        zone = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone["open_assignments"], 0)
        self.assertEqual(zone["pending_reports"], 0)
        closed = self.service.close_zone(
            zone["id"], {"expected_version": zone["version"]}, "ic", "incident_commander")
        self.assertEqual(closed["status"], "closed")

    def test_evacuated_batch_closes_occupancy_then_zone_can_close(self):
        self.upload([
            self.event("departed", "2026-09-25T08:00:00+00:00"),
            self.event("arrived", "2026-09-25T09:00:00+00:00"),
        ])
        zone = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone["open_assignments"], 1)
        self.upload([self.event("evacuated", "2026-09-25T18:00:00+00:00")])
        zone = self.service.get_zone(self.zone["id"], "viewer")
        self.assertEqual(zone["open_assignments"], 0)
        closed = self.service.close_zone(
            zone["id"], {"expected_version": zone["version"]}, "ic", "incident_commander")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["close_blockers"], [])
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
