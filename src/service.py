from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_number, require_text, canonical_event_time)
from .repository import Repository
from .rules import (ASSIGN_ROLES, AUDIT_ROLES, CREATE_ROLES, ENTITY,
                    OFFLINE_CONFIRM_ROLES, OFFLINE_UPLOAD_ROLES,
                    RECORD_ROLES, RESOURCE_CREATE_ROLES, RESOURCE_KINDS,
                    TITLE, VIEW_ROLES, ZONE_CREATE_ROLES, ZONE_VIEW_ROLES,
                    completion_blockers, escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    role_for_zone_transition, validate_transition,
                    validate_zone_transition, zone_closure_blockers)


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

    # ---- 任务区与资源 ----
    def create_zone(self, item_id: int, payload: Dict[str, Any],
                    actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        if not isinstance(item_id, int):
            from .domain import ValidationError
            raise ValidationError("任务区id必须是整数")
        code = require_text(payload.get("code"), "code", 60)
        name = require_text(payload.get("name"), "name", 200)
        zone = self.repository.create_zone(item_id, code, name, actor)
        self.repository.append_audit("zone_create", "zone", zone["id"], actor, {
            "item_id": item_id, "code": code, "name": name,
        })
        return self.enrich_zone(zone)

    def get_zone(self, zone_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_VIEW_ROLES)
        return self.enrich_zone(self.repository.get_zone(zone_id))

    def list_zones(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, ZONE_VIEW_ROLES)
        return [self.enrich_zone(zone) for zone in self.repository.list_zones(item_id)]

    def close_zone(self, zone_id: int, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        zone = self.repository.get_zone(zone_id)
        validate_zone_transition(zone["status"], "closed")
        ensure_role(role, role_for_zone_transition("closed"))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = zone_closure_blockers(
            self.repository.active_occupancy_count(zone_id),
            self.repository.pending_offline_count(zone_id),
        )
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_zone(zone_id, "closed", expected_version, actor)
        self.repository.append_audit("zone_close", "zone", zone_id, actor, {
            "from": zone["status"], "to": "closed",
        })
        return self.enrich_zone(updated)

    def create_resource(self, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        code = require_text(payload.get("code"), "code", 60)
        name = require_text(payload.get("name"), "name", 200)
        kind = payload.get("kind")
        if kind not in RESOURCE_KINDS:
            from .domain import ValidationError
            raise ValidationError("kind必须是team或vehicle")
        resource = self.repository.create_resource(code, name, kind, actor)
        self.repository.append_audit("resource_create", "resource", resource["id"], actor, {
            "code": code, "name": name, "kind": kind,
        })
        return resource

    def get_resource(self, resource_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_VIEW_ROLES)
        return self.repository.get_resource(resource_id)

    def list_resources(self, role: str, kind: Optional[str] = None) -> list:
        ensure_role(role, ZONE_VIEW_ROLES)
        if kind is not None and kind not in RESOURCE_KINDS:
            from .domain import ValidationError
            raise ValidationError("kind必须是team或vehicle")
        return self.repository.list_resources(kind)

    def assign(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ASSIGN_ROLES)
        actor = require_text(actor, "actor", 100)
        zone_id = payload.get("zone_id")
        resource_id = payload.get("resource_id")
        if not isinstance(zone_id, int) or not isinstance(resource_id, int):
            from .domain import ValidationError
            raise ValidationError("zone_id和resource_id必须是整数")
        arrived_at = canonical_event_time(payload.get("arrived_at"), "arrived_at")
        withdrawn_at = None
        if payload.get("withdrawn_at") is not None:
            withdrawn_at = canonical_event_time(payload.get("withdrawn_at"), "withdrawn_at")
        try:
            occupancy = self.repository.assign_occupancy(
                zone_id, resource_id, arrived_at, withdrawn_at, "dispatch", actor)
        except ConflictError as exc:
            self.repository.append_audit("assign_blocked", "resource", resource_id, actor, {
                "zone_id": zone_id, "conflicts": (exc.details or {}).get("conflicts", []),
            })
            raise
        self.repository.append_audit("assign", "occupancy", occupancy["id"], actor, {
            "zone_id": zone_id, "resource_id": resource_id,
            "arrived_at": arrived_at, "withdrawn_at": withdrawn_at,
        })
        return occupancy

    def withdraw(self, occupancy_id: int, payload: Dict[str, Any],
                 actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, ASSIGN_ROLES)
        actor = require_text(actor, "actor", 100)
        withdrawn_at = canonical_event_time(payload.get("withdrawn_at"), "withdrawn_at")
        occupancy = self.repository.withdraw_occupancy(occupancy_id, withdrawn_at, actor)
        self.repository.append_audit("withdraw", "occupancy", occupancy_id, actor, {
            "zone_id": occupancy["zone_id"], "resource_id": occupancy["resource_id"],
            "withdrawn_at": withdrawn_at,
        })
        return occupancy

    def list_occupancies(self, role: str, zone_id: Optional[int] = None,
                         resource_id: Optional[int] = None) -> list:
        ensure_role(role, ZONE_VIEW_ROLES)
        return self.repository.list_occupancies(zone_id, resource_id)

    # ---- 离线回传 ----
    def upload_offline(self, payload: Dict[str, Any],
                       actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OFFLINE_UPLOAD_ROLES)
        actor = require_text(actor, "actor", 100)
        batch_no = require_text(payload.get("batch_no"), "batch_no", 100)
        existing = self.repository.get_offline_batch_by_no(batch_no)
        if existing is not None:
            zone_id = payload.get("zone_id", existing["zone_id"])
            resource_id = payload.get("resource_id", existing["resource_id"])
        else:
            zone_id = payload.get("zone_id")
            resource_id = payload.get("resource_id")
        if not isinstance(zone_id, int) or not isinstance(resource_id, int):
            from .domain import ValidationError
            raise ValidationError("zone_id和resource_id必须是整数")
        reports = payload.get("reports")
        if not isinstance(reports, list) or not reports:
            from .domain import ValidationError
            raise ValidationError("reports必须是非空数组")
        normalized = []
        for report in reports:
            if not isinstance(report, dict):
                from .domain import ValidationError
                raise ValidationError("每条回传必须是对象")
            status = report.get("status")
            event_time = canonical_event_time(report.get("event_time"), "event_time")
            uploaded_by = report.get("uploaded_by")
            if uploaded_by is not None:
                uploaded_by = require_text(uploaded_by, "uploaded_by", 100)
            normalized.append({"phase": status, "event_time": event_time,
                               "uploaded_by": uploaded_by})
        batch = self.repository.merge_offline_batch(
            batch_no, zone_id, resource_id, normalized, actor)
        self.repository.append_audit("offline_upload", "offline_batch", batch["id"], actor, {
            "batch_no": batch_no, "zone_id": zone_id, "resource_id": resource_id,
            "reports": len(normalized),
            "pending": len(batch["pending"]),
        })
        return batch

    def confirm_offline(self, batch_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, OFFLINE_CONFIRM_ROLES)
        actor = require_text(actor, "actor", 100)
        index = payload.get("index")
        if not isinstance(index, int) or index < 0:
            from .domain import ValidationError
            raise ValidationError("index必须是非负整数")
        accepted = payload.get("accepted")
        if not isinstance(accepted, bool):
            from .domain import ValidationError
            raise ValidationError("accepted必须是布尔值")
        result = self.repository.resolve_offline_pending(batch_id, index, accepted, actor)
        resolved = result.pop("resolved")
        self.repository.append_audit("offline_confirm", "offline_batch", batch_id, actor, {
            "index": index, "accepted": accepted,
            "phase": resolved["entry"]["phase"],
            "event_time": resolved["entry"]["event_time"],
        })
        return result

    def get_offline_batch(self, batch_id: int, role: str) -> Dict[str, Any]:
        ensure_role(role, ZONE_VIEW_ROLES)
        return self.repository.get_offline_batch(batch_id)

    def list_offline_batches(self, role: str, zone_id: Optional[int] = None) -> list:
        ensure_role(role, ZONE_VIEW_ROLES)
        return self.repository.list_offline_batches(zone_id)

    def enrich_zone(self, zone: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(zone)
        result["active_occupancies"] = self.repository.active_occupancy_count(zone["id"])
        result["pending_offline"] = self.repository.pending_offline_count(zone["id"])
        return result

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
