from __future__ import annotations

from typing import Any, Dict, List, Optional

from .audit import utc_now
from .domain import (ConflictError, NotFoundError, OFFLINE_EVENTS,
                     ValidationError, ensure_role, normalize_severity,
                     parse_event_time, require_number, require_text,
                     to_event_time)
from .repository import Repository
from .rules import (AUDIT_ROLES, ASSIGNMENT_ENTITY, CREATE_ROLES,
                    DISPATCH_ROLES, ENTITY, OFFLINE_UPLOAD_ROLES, RECORD_ROLES,
                    REPORT_ENTITY, REPORT_REVIEW_ROLES, RESOURCE_ENTITY, TITLE,
                    VIEW_ROLES, ZONE_CREATE_ROLES, ZONE_ENTITY,
                    completion_blockers, earliest, escalation_required,
                    ordering_conflicts, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition, zone_close_blockers)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ---- 任务区 ----
    @staticmethod
    def _require_id(value: Any, field: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValidationError(f"{field}必须是正整数")
        return value

    def _active_zone(self, zone_id: int) -> Dict[str, Any]:
        zone = self.repository.get_zone(zone_id)
        if zone["status"] != "active":
            raise ConflictError("任务区已关闭，不能再进行该操作")
        return zone

    def create_zone(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        description = payload.get("description", "") or ""
        if not isinstance(description, str) or len(description) > 2000:
            raise ValidationError("description不能超过2000个字符")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        zone = self.repository.create_zone(name, description, external_ref, actor)
        self.repository.append_audit("create", ZONE_ENTITY, zone["id"], actor, {
            "name": name, "external_ref": external_ref,
        })
        return self.enrich_zone(zone)

    def get_zone(self, zone_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich_zone(self.repository.get_zone(zone_id))

    def list_zones(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        if status is not None and status not in ("active", "closed"):
            raise ValidationError("status必须是active或closed")
        return [self.enrich_zone(z) for z in self.repository.list_zones(status)]

    def list_zone_assignments(self, zone_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_zone(zone_id)
        return self.repository.list_assignments(zone_id=zone_id)

    def close_zone(self, zone_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        expected_version = self._require_id(payload.get("expected_version"), "expected_version")
        zone = self.repository.get_zone(zone_id)
        # 关闭前：未撤离资源和未确认记录都不能被忽略
        blockers = zone_close_blockers(
            self.repository.open_assignment_count(zone_id),
            self.repository.pending_report_count(zone_id),
        )
        if blockers:
            raise ConflictError("任务区无法关闭：" + "；".join(blockers),
                                details={"blockers": blockers})
        updated = self.repository.close_zone(zone_id, expected_version, actor)
        self.repository.append_audit("close", ZONE_ENTITY, zone_id, actor, {
            "name": zone["name"], "from": zone["status"], "to": "closed",
        })
        return self.enrich_zone(updated)

    def enrich_zone(self, zone: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(zone)
        result["open_assignments"] = self.repository.open_assignment_count(zone["id"])
        result["pending_reports"] = self.repository.pending_report_count(zone["id"])
        result["close_blockers"] = zone_close_blockers(
            result["open_assignments"], result["pending_reports"])
        return result

    # ---- 资源（救援队/车辆）----
    def create_resource(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        name = require_text(payload.get("name"), "name", 200)
        resource = self.repository.create_resource(code, kind, name, actor)
        self.repository.append_audit("create", RESOURCE_ENTITY, resource["id"], actor, {
            "code": code, "kind": kind, "name": name,
        })
        return resource

    def list_resources(self, role: str) -> list:
        self._view(role)
        return self.repository.list_resources()

    def list_resource_assignments(self, resource_id: int, role: str) -> list:
        self._view(role)
        self.repository.get_resource(resource_id)
        return self.repository.list_assignments(resource_id=resource_id)

    # ---- 派单与撤离 ----
    def dispatch(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        resource_id = self._require_id(payload.get("resource_id"), "resource_id")
        zone_id = self._require_id(payload.get("zone_id"), "zone_id")
        start = parse_event_time(payload.get("start_at", utc_now()), "start_at")
        end_value = payload.get("end_at")
        end = parse_event_time(end_value, "end_at") if end_value is not None else None
        if end is not None and end <= start:
            raise ValidationError("end_at必须晚于start_at")
        self.repository.get_resource(resource_id)
        self._active_zone(zone_id)
        # 同一资源时段重叠：指出冲突任务区并挡住这次分配
        conflicts = self.repository.find_conflicting_assignments(
            resource_id, to_event_time(start),
            to_event_time(end) if end else None)
        if conflicts:
            raise ConflictError("资源在该时段已被其他任务区占用", details={
                "conflicts": [{
                    "zone_id": c["zone_id"], "zone_name": c["zone_name"],
                    "start_at": c["start_at"], "end_at": c["end_at"],
                    "source": c["source"],
                } for c in conflicts],
            })
        assignment = self.repository.create_assignment(
            resource_id, zone_id, to_event_time(start), "dispatch", None, actor,
            end_at=to_event_time(end) if end else None)
        if end is None:
            self.repository.set_resource_status(resource_id, "deployed")
        self.repository.append_audit("dispatch", ASSIGNMENT_ENTITY, assignment["id"], actor, {
            "resource_id": resource_id, "zone_id": zone_id,
            "start_at": assignment["start_at"], "end_at": assignment["end_at"],
        })
        return assignment

    def release(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        resource_id = self._require_id(payload.get("resource_id"), "resource_id")
        zone_id = self._require_id(payload.get("zone_id"), "zone_id")
        end = parse_event_time(payload.get("end_at", utc_now()), "end_at")
        self.repository.get_resource(resource_id)
        self.repository.get_zone(zone_id)
        open_assignment = self.repository.find_open_assignment(resource_id, zone_id)
        if open_assignment is None:
            raise NotFoundError("该资源在任务区没有未撤离的占用")
        if end < parse_event_time(open_assignment["start_at"], "start_at"):
            raise ValidationError("撤离时间不能早于到场时间")
        assignment = self.repository.close_assignment(open_assignment["id"], to_event_time(end))
        if self.repository.find_open_assignment(resource_id) is None:
            self.repository.set_resource_status(resource_id, "evacuated")
        self.repository.append_audit("release", ASSIGNMENT_ENTITY, assignment["id"], actor, {
            "resource_id": resource_id, "zone_id": zone_id, "end_at": assignment["end_at"],
        })
        return assignment

    # ---- 离线批次回传 ----
    def upload_offline_batch(self, payload: Dict[str, Any], actor: str,
                             role: str) -> Dict[str, Any]:
        ensure_role(role, OFFLINE_UPLOAD_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        events = payload.get("events")
        if not isinstance(events, list) or not events:
            raise ValidationError("events必须是非空数组")

        rows: List[Dict[str, Any]] = []
        for index, event in enumerate(events):
            if not isinstance(event, dict):
                raise ValidationError(f"events[{index}]必须是对象")
            event_type = event.get("event")
            if event_type not in OFFLINE_EVENTS:
                raise ValidationError(f"events[{index}].event必须是{','.join(OFFLINE_EVENTS)}之一")
            rows.append({
                "event": event_type,
                "resource_id": self._require_id(event.get("resource_id"), f"events[{index}].resource_id"),
                "zone_id": self._require_id(event.get("zone_id"), f"events[{index}].zone_id"),
                "field_time": parse_event_time(event.get("field_time"), f"events[{index}].field_time"),
            })
        # 同一批次号必须描述同一资源在同一任务区的班次
        signature = {(r["resource_id"], r["zone_id"]) for r in rows}
        if len(signature) != 1:
            raise ValidationError("同一批次号的事件必须属于同一资源和同一任务区")
        resource_id, zone_id = next(iter(signature))
        events_by_type: Dict[str, list] = {}
        for r in rows:
            events_by_type.setdefault(r["event"], []).append(r)
        dup_types = [k for k, v in events_by_type.items() if len(v) > 1]
        if dup_types:
            raise ValidationError(f"批次内事件类型重复: {','.join(sorted(dup_types))}")
        existing = self.repository.get_batch_reports(batch_no)
        if existing:
            sig_existing = {(r["resource_id"], r["zone_id"]) for r in existing}
            if sig_existing != signature:
                raise ConflictError("批次号已用于其他资源或任务区，不能合并")
        self.repository.get_resource(resource_id)
        self._active_zone(zone_id)

        merged: List[Dict[str, Any]] = []
        for row in rows:
            current = self.repository.get_report(batch_no, row["event"])
            field_text = to_event_time(row["field_time"])
            if current is None:
                merged.append(self.repository.insert_report(
                    batch_no, row["event"], resource_id, zone_id, field_text,
                    "confirmed", None, actor))
                continue
            # 重复上传：已驳回的记录不再被新上传悄悄改判
            if current["review_status"] == "rejected":
                merged.append(current)
                continue
            # 重复上传按最早的现场时间合并；pending维持待确认，由后续统一时序校验复判
            earliest_time = earliest([parse_event_time(current["field_time"], "field_time"),
                                      row["field_time"]])
            merged.append(self.repository.update_report(
                current["id"], field_time=to_event_time(earliest_time),
                review_status=current["review_status"], conflict_note=current["conflict_note"],
                increment_upload=True))

        # 合并后重新做批次内时序校验：后到的矛盾状态标出来等调度员确认
        latest_by_event = {r["event"]: r for r in self.repository.get_batch_reports(batch_no)}
        times = {k: parse_event_time(v["field_time"], "field_time")
                 for k, v in latest_by_event.items()}
        bad = set(ordering_conflicts(times))
        # 没有到场就撤离（包括只回传撤离）同样是矛盾状态
        if "evacuated" in times and "arrived" not in times:
            bad.add("evacuated")
        for event_type, report in latest_by_event.items():
            if event_type in bad:
                if report["review_status"] != "rejected":
                    self.repository.update_report(
                        report["id"], review_status="pending",
                        conflict_note="现场时间与批次内其他事件矛盾，待调度员确认")
            elif report["review_status"] == "pending" and event_type not in bad:
                # 重复上传消除了矛盾：恢复确认并同步占用
                self.repository.update_report(
                    report["id"], review_status="confirmed", conflict_note=None)
        final_reports = self.repository.get_batch_reports(batch_no)
        self._sync_offline_occupancy(batch_no)
        self.repository.append_audit("offline_batch", REPORT_ENTITY, 0, actor, {
            "batch_no": batch_no, "resource_id": resource_id, "zone_id": zone_id,
            "events": [r["event"] for r in rows],
            "pending": [r["event"] for r in final_reports if r["review_status"] == "pending"],
        })
        return {
            "batch_no": batch_no, "resource_id": resource_id, "zone_id": zone_id,
            "reports": final_reports,
            "has_contradiction": any(r["review_status"] == "pending" for r in final_reports),
        }

    def list_offline_reports(self, role: str, review_status: Optional[str] = None,
                             zone_id: Optional[int] = None) -> list:
        self._view(role)
        if review_status is not None and review_status not in ("confirmed", "pending", "rejected"):
            raise ValidationError("review_status必须是confirmed/pending/rejected之一")
        return self.repository.list_reports(review_status, zone_id)

    def review_offline_report(self, report_id: int, payload: Dict[str, Any],
                              actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, REPORT_REVIEW_ROLES)
        actor = require_text(actor, "actor", 100)
        decision = payload.get("decision")
        if decision not in ("confirm", "reject"):
            raise ValidationError("decision必须是confirm或reject")
        report = self.repository.get_report_by_id(report_id)
        batch_no = report["batch_no"]
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        self.repository.update_report(
            report_id,
            review_status="confirmed" if decision == "confirm" else "rejected",
            conflict_note=None if decision == "confirm" else (note or "调度员驳回该矛盾状态"))
        self._sync_offline_occupancy(batch_no)
        self.repository.append_audit("review", REPORT_ENTITY, report_id, actor, {
            "batch_no": batch_no, "event": report["event"], "decision": decision,
        })
        return self.repository.get_report_by_id(report_id)

    def _sync_offline_occupancy(self, batch_no: str) -> None:
        """按批次内已确认记录重建该资源-任务区的占用时段；pending/rejected不产生占用。"""
        reports = [r for r in self.repository.get_batch_reports(batch_no)
                   if r["review_status"] == "confirmed"]
        if not reports:
            return
        by_event = {r["event"]: r for r in reports}
        resource_id = reports[0]["resource_id"]
        zone_id = reports[0]["zone_id"]
        departed = by_event.get("departed")
        arrived = by_event.get("arrived")
        evacuated = by_event.get("evacuated")

        open_assignment = self.repository.find_open_assignment(resource_id, zone_id)

        if evacuated is not None:
            # 已撤离：形成闭合占用区间（没有到场/出发支撑的撤离仍处于pending，不会进入这里）
            start = arrived or departed
            if start is None:
                return
            end_time = parse_event_time(evacuated["field_time"], "field_time")
            # 同一班次既可能由调度台派单、也可能由离线回传表达：开放占用在撤离时点之前
            # 开始，即视为同一现场停留，撤离回传应把它闭合
            if (open_assignment is not None
                    and open_assignment["start_at"] <= start["field_time"]
                    and open_assignment["start_at"] <= to_event_time(end_time)):
                self.repository.close_assignment(open_assignment["id"], to_event_time(end_time))
            else:
                conflicts = self.repository.find_conflicting_assignments(
                    resource_id, start["field_time"], to_event_time(end_time))
                if not conflicts:
                    self.repository.create_assignment(
                        resource_id, zone_id, start["field_time"], "offline", batch_no,
                        evacuated["created_by"])
                    created = self.repository.find_open_assignment(resource_id, zone_id)
                    if created is not None:
                        self.repository.close_assignment(created["id"], to_event_time(end_time))
            if self.repository.find_open_assignment(resource_id) is None:
                self.repository.set_resource_status(resource_id, "evacuated")
            return

        start = arrived or departed
        if start is None:
            return
        if arrived is None:
            # 仅有出发、尚未到场：不生成现场占用；仅撤掉本批次自己早先生成的占位
            if (open_assignment is not None and open_assignment["source"] == "offline"
                    and open_assignment["batch_no"] == batch_no):
                self.repository.delete_assignment(open_assignment["id"])
            return
        if open_assignment is not None:
            # 已存在（派单或其他批次）的在场占用，沿用
            return
        # 到场未撤离：占用开放区间，派单将与之检测到重叠
        self.repository.create_assignment(
            resource_id, zone_id, start["field_time"], "offline", batch_no,
            arrived["created_by"])
        self.repository.set_resource_status(resource_id, "deployed")

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
