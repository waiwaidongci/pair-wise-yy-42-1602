from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ZONE_STATES
from .rules import ID_PREFIX, STATES

# 区分"未传conflict_note"与"显式置空"的哨兵
_REPORT_NOTE_UNSET = object()


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS zones (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','closed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closed_at TEXT,
                    UNIQUE(name)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_zones_external_ref
                    ON zones(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'available'
                        CHECK(status IN ('available','deployed','evacuated')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assignments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    zone_id INTEGER NOT NULL REFERENCES zones(id),
                    start_at TEXT NOT NULL,
                    end_at TEXT,
                    source TEXT NOT NULL DEFAULT 'dispatch'
                        CHECK(source IN ('dispatch','offline')),
                    batch_no TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_assignments_resource
                    ON assignments(resource_id, start_at);
                CREATE INDEX IF NOT EXISTS ix_assignments_zone ON assignments(zone_id);
                CREATE TABLE IF NOT EXISTS offline_reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL,
                    event TEXT NOT NULL CHECK(event IN ('departed','arrived','evacuated')),
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    zone_id INTEGER NOT NULL REFERENCES zones(id),
                    field_time TEXT NOT NULL,
                    review_status TEXT NOT NULL DEFAULT 'confirmed'
                        CHECK(review_status IN ('confirmed','pending','rejected')),
                    conflict_note TEXT,
                    upload_count INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(batch_no, event)
                );
                CREATE INDEX IF NOT EXISTS ix_offline_reports_review
                    ON offline_reports(review_status);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 任务区 ----
    def create_zone(self, name: str, description: str, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO zones(name, description, status, version, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (name, description, ZONE_STATES[0], 1, external_ref, actor, now),
                )
                zone_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务区名称或external_ref已存在") from exc
        return self.get_zone(zone_id)

    def get_zone(self, zone_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return dict(row)

    def list_zones(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM zones"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def close_zone(self, zone_id: int, expected_version: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE zones SET status='closed', version=version+1, closed_at=?
                   WHERE id=? AND version=? AND status='active'""",
                (now, zone_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("任务区不存在")
                raise ConflictError("版本冲突或任务区已关闭，请刷新后重试")
        return self.get_zone(zone_id)

    def open_assignment_count(self, zone_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM assignments WHERE zone_id=? AND end_at IS NULL",
                (zone_id,),
            ).fetchone()
        return int(row["n"])

    def pending_report_count(self, zone_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM offline_reports WHERE zone_id=? AND review_status='pending'",
                (zone_id,),
            ).fetchone()
        return int(row["n"])

    # ---- 资源 ----
    def create_resource(self, code: str, kind: str, name: str, actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO resources(code, kind, name, status, created_by, created_at)
                       VALUES(?,?,?, 'available', ?,?)""",
                    (code, kind, name, actor, now),
                )
                resource_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("资源编号已存在") from exc
        return self.get_resource(resource_id)

    def get_resource(self, resource_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        return dict(row)

    def list_resources(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM resources ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def set_resource_status(self, resource_id: int, status: str) -> None:
        with self._lock, self.conn:
            self.conn.execute("UPDATE resources SET status=? WHERE id=?", (status, resource_id))

    # ---- 资源占用（派单 / 撤离 / 离线同步）----
    def create_assignment(self, resource_id: int, zone_id: int, start_at: str,
                          source: str, batch_no: Optional[str], actor: str,
                          end_at: Optional[str] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO assignments(resource_id, zone_id, start_at, end_at, source,
                   batch_no, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (resource_id, zone_id, start_at, end_at, source, batch_no, actor, now),
            )
            assignment_id = int(cur.lastrowid)
            row = self.conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        return dict(row)

    def find_open_assignment(self, resource_id: int, zone_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        sql = "SELECT * FROM assignments WHERE resource_id=? AND end_at IS NULL"
        params: list = [resource_id]
        if zone_id is not None:
            sql += " AND zone_id=?"
            params.append(zone_id)
        sql += " ORDER BY start_at DESC LIMIT 1"
        with self._lock:
            row = self.conn.execute(sql, tuple(params)).fetchone()
        return dict(row) if row else None

    def find_conflicting_assignments(self, resource_id: int, start_at: str,
                                     end_at: Optional[str], exclude_id: Optional[int] = None) -> List[Dict[str, Any]]:
        """半开区间[start,end)重叠；end为NULL表示仍在场（右开放）。端点相接不冲突。"""
        clauses = ["a.resource_id=?"]
        params: list = [resource_id]
        if end_at is not None:
            # 既有区间在新区间结束前已开始：a.start < new.end
            clauses.append("a.start_at < ?")
            params.append(end_at)
        # 新开始时既有区间尚未结束（既有end为空表示仍在场）
        clauses.append("(a.end_at IS NULL OR ? < a.end_at)")
        params.append(start_at)
        sql = ("SELECT a.*, z.name AS zone_name FROM assignments a "
               "JOIN zones z ON z.id=a.zone_id WHERE " + " AND ".join(clauses))
        if exclude_id is not None:
            sql += " AND a.id<>?"
            params.append(exclude_id)
        sql += " ORDER BY a.start_at"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def close_assignment(self, assignment_id: int, end_at: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            cur = self.conn.execute(
                "UPDATE assignments SET end_at=? WHERE id=? AND end_at IS NULL",
                (end_at, assignment_id),
            )
            if cur.rowcount == 0:
                raise ConflictError("占用已结束或不存在")
            row = self.conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
        return dict(row)

    def list_assignments(self, zone_id: Optional[int] = None,
                         resource_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT a.*, z.name AS zone_name, r.code AS resource_code, r.name AS resource_name "
               "FROM assignments a JOIN zones z ON z.id=a.zone_id "
               "JOIN resources r ON r.id=a.resource_id")
        clauses=[]; params: list=[]
        if zone_id is not None:
            clauses.append("a.zone_id=?"); params.append(zone_id)
        if resource_id is not None:
            clauses.append("a.resource_id=?"); params.append(resource_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY a.start_at"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def delete_assignment(self, assignment_id: int) -> None:
        with self._lock, self.conn:
            self.conn.execute("DELETE FROM assignments WHERE id=?", (assignment_id,))

    # ---- 离线批次回传 ----
    def get_report(self, batch_no: str, event: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM offline_reports WHERE batch_no=? AND event=?",
                (batch_no, event),
            ).fetchone()
        return dict(row) if row else None

    def get_batch_reports(self, batch_no: str) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM offline_reports WHERE batch_no=? ORDER BY field_time, id",
                (batch_no,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_reports(self, review_status: Optional[str] = None,
                     zone_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = ("SELECT o.*, r.code AS resource_code, z.name AS zone_name "
               "FROM offline_reports o JOIN resources r ON r.id=o.resource_id "
               "JOIN zones z ON z.id=o.zone_id")
        clauses=[]; params: list=[]
        if review_status:
            clauses.append("o.review_status=?"); params.append(review_status)
        if zone_id is not None:
            clauses.append("o.zone_id=?"); params.append(zone_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY o.batch_no, o.field_time, o.id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def insert_report(self, batch_no: str, event: str, resource_id: int, zone_id: int,
                      field_time: str, review_status: str, conflict_note: Optional[str],
                      actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """INSERT INTO offline_reports(batch_no, event, resource_id, zone_id, field_time,
                   review_status, conflict_note, upload_count, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,1,?,?,?)""",
                (batch_no, event, resource_id, zone_id, field_time, review_status,
                 conflict_note, actor, now, now),
            )
            report_id = int(cur.lastrowid)
        return self.get_report_by_id(report_id)

    def update_report(self, report_id: int, field_time: Optional[str] = None,
                      review_status: Optional[str] = None,
                      conflict_note: Optional[str] = _REPORT_NOTE_UNSET,
                      increment_upload: bool = False) -> Dict[str, Any]:
        sets=[]; params: list=[]
        if field_time is not None:
            sets.append("field_time=?"); params.append(field_time)
        if review_status is not None:
            sets.append("review_status=?"); params.append(review_status)
        if conflict_note is not _REPORT_NOTE_UNSET:
            sets.append("conflict_note=?"); params.append(conflict_note)
        if increment_upload:
            sets.append("upload_count=upload_count+1")
        if not sets:
            return self.get_report_by_id(report_id)
        sets.append("updated_at=?"); params.append(utc_now())
        params.append(report_id)
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"UPDATE offline_reports SET {', '.join(sets)} WHERE id=?", tuple(params))
            if cur.rowcount == 0:
                raise NotFoundError("离线记录不存在")
        return self.get_report_by_id(report_id)

    def get_report_by_id(self, report_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM offline_reports WHERE id=?", (report_id,)).fetchone()
        if row is None:
            raise NotFoundError("离线记录不存在")
        return dict(row)

    def close(self) -> None:
        with self._lock:
            self.conn.close()