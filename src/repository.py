from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, parse_event_time
from .rules import ID_PREFIX, OFFLINE_PHASES, STATES


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
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    code TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','closed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, code)
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('team','vehicle')),
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS occupancies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    zone_id INTEGER NOT NULL REFERENCES zones(id) ON DELETE CASCADE,
                    resource_id INTEGER NOT NULL REFERENCES resources(id) ON DELETE CASCADE,
                    arrived_at TEXT NOT NULL,
                    withdrawn_at TEXT,
                    source TEXT NOT NULL DEFAULT 'dispatch',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_occupancies_resource ON occupancies(resource_id);
                CREATE INDEX IF NOT EXISTS ix_occupancies_zone ON occupancies(zone_id);
                CREATE TABLE IF NOT EXISTS offline_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    zone_id INTEGER REFERENCES zones(id) ON DELETE CASCADE,
                    resource_id INTEGER REFERENCES resources(id) ON DELETE CASCADE,
                    phases TEXT NOT NULL DEFAULT '{{}}',
                    pending TEXT NOT NULL DEFAULT '[]',
                    occupancy_id INTEGER REFERENCES occupancies(id) ON DELETE SET NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
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

    # ---- 任务区与资源 ----
    @staticmethod
    def _overlaps(start, end, arrived, withdrawn):
        # None 表示尚未撤离，时间上视为无限远
        if end is None:
            end = datetime.max.replace(tzinfo=timezone.utc)
        if withdrawn is None:
            withdrawn = datetime.max.replace(tzinfo=timezone.utc)
        return arrived < end and withdrawn > start

    def create_zone(self, item_id: int, code: str, name: str,
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO zones(item_id, code, name, status, version,
                       created_by, created_at, updated_at) VALUES(?,?,?,'active',1,?,?,?)""",
                    (item_id, code, name, actor, now, now),
                )
                zone_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("任务区编号在该事件下已存在") from exc
        return self.get_zone(zone_id)

    def get_zone(self, zone_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM zones WHERE id=?", (zone_id,)).fetchone()
        if row is None:
            raise NotFoundError("任务区不存在")
        return dict(row)

    def list_zones(self, item_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM zones"
        params: tuple = ()
        if item_id is not None:
            sql += " WHERE item_id=?"
            params = (item_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def transition_zone(self, zone_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE zones SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, zone_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM zones WHERE id=?", (zone_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("任务区不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_zone(zone_id)

    def create_resource(self, code: str, name: str, kind: str,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO resources(code, name, kind, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (code, name, kind, actor, now),
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

    def list_resources(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM resources"
        params: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def assign_occupancy(self, zone_id: int, resource_id: int,
                         arrived_at: str, withdrawn_at: Optional[str],
                         source: str, actor: str) -> Dict[str, Any]:
        zone = self.get_zone(zone_id)
        if zone["status"] != "active":
            raise ConflictError("任务区已关闭，不能再分配资源")
        self.get_resource(resource_id)
        start = parse_event_time(arrived_at, "arrived_at")
        end = parse_event_time(withdrawn_at, "withdrawn_at") if withdrawn_at else None
        with self._lock, self.conn:
            rows = self.conn.execute(
                """SELECT o.*, z.code AS zone_code, z.name AS zone_name
                   FROM occupancies o JOIN zones z ON z.id=o.zone_id
                   WHERE o.resource_id=?""",
                (resource_id,),
            ).fetchall()
            conflicts = []
            for row in rows:
                row_start = parse_event_time(row["arrived_at"], "arrived_at")
                row_end = parse_event_time(row["withdrawn_at"], "withdrawn_at") if row["withdrawn_at"] else None
                if self._overlaps(start, end, row_start, row_end):
                    conflicts.append({
                        "occupancy_id": row["id"], "zone_id": row["zone_id"],
                        "zone_code": row["zone_code"], "zone_name": row["zone_name"],
                        "arrived_at": row["arrived_at"], "withdrawn_at": row["withdrawn_at"],
                    })
            if conflicts:
                raise ConflictError(
                    "资源在重叠时段已被占用，冲突任务区：" + ",".join(c["zone_code"] for c in conflicts),
                    {"conflicts": conflicts},
                )
            now = utc_now()
            cur = self.conn.execute(
                """INSERT INTO occupancies(zone_id, resource_id, arrived_at, withdrawn_at,
                   source, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                (zone_id, resource_id, arrived_at, withdrawn_at, source, actor, now),
            )
            occupancy_id = int(cur.lastrowid)
        return self.get_occupancy(occupancy_id)

    def get_occupancy(self, occupancy_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM occupancies WHERE id=?", (occupancy_id,)).fetchone()
        if row is None:
            raise NotFoundError("占用记录不存在")
        return dict(row)

    def withdraw_occupancy(self, occupancy_id: int, withdrawn_at: str,
                           actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM occupancies WHERE id=?", (occupancy_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("占用记录不存在")
            if row["withdrawn_at"] is not None:
                raise ConflictError("资源已经撤离")
            if parse_event_time(withdrawn_at, "withdrawn_at") < parse_event_time(row["arrived_at"], "arrived_at"):
                raise ConflictError("撤离时间不能早于到场时间")
            self.conn.execute(
                "UPDATE occupancies SET withdrawn_at=? WHERE id=?",
                (withdrawn_at, occupancy_id),
            )
        return self.get_occupancy(occupancy_id)

    def list_occupancies(self, zone_id: Optional[int] = None,
                         resource_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM occupancies"
        clauses = []
        params: list = []
        if zone_id is not None:
            clauses.append("zone_id=?"); params.append(zone_id)
        if resource_id is not None:
            clauses.append("resource_id=?"); params.append(resource_id)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def active_occupancy_count(self, zone_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM occupancies WHERE zone_id=? AND withdrawn_at IS NULL",
                (zone_id,),
            ).fetchone()
        return int(row["n"])

    def pending_offline_count(self, zone_id: int) -> int:
        with self._lock:
            rows = self.conn.execute(
                "SELECT pending FROM offline_batches WHERE zone_id=?", (zone_id,)
            ).fetchall()
        return sum(len(json.loads(row["pending"])) for row in rows)

    # ---- 离线批次 ----
    def get_offline_batch_by_no(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM offline_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
        if row is None:
            return None
        batch = dict(row)
        batch["phases"] = json.loads(batch["phases"])
        batch["pending"] = json.loads(batch["pending"])
        return batch

    def get_offline_batch(self, batch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM offline_batches WHERE id=?", (batch_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("离线批次不存在")
        batch = dict(row)
        batch["phases"] = json.loads(batch["phases"])
        batch["pending"] = json.loads(batch["pending"])
        return batch

    def list_offline_batches(self, zone_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM offline_batches"
        params: tuple = ()
        if zone_id is not None:
            sql += " WHERE zone_id=?"
            params = (zone_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            batch = dict(row)
            batch["phases"] = json.loads(batch["phases"])
            batch["pending"] = json.loads(batch["pending"])
            result.append(batch)
        return result

    @staticmethod
    def _dt(text: str):
        return parse_event_time(text, "event_time")

    def _reconcile_occupancy(self, zone_id: int, resource_id: int,
                             occupancy_id: Optional[int], phases: Dict[str, str],
                             actor: str) -> int:
        """根据已合并的阶段记录同步资源占用（同连接、同锁内调用）。"""
        if occupancy_id is not None:
            self.conn.execute(
                """UPDATE occupancies SET arrived_at=?, withdrawn_at=?
                   WHERE id=?""",
                (phases.get("arrived"), phases.get("withdrawn"), occupancy_id),
            )
            return occupancy_id
        arrived = phases.get("arrived")
        if arrived is None:
            return None
        cur = self.conn.execute(
            """INSERT INTO occupancies(zone_id, resource_id, arrived_at, withdrawn_at,
               source, created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
            (zone_id, resource_id, arrived, phases.get("withdrawn"), "offline", actor, utc_now()),
        )
        return int(cur.lastrowid)

    def merge_offline_batch(self, batch_no: str, zone_id: int, resource_id: int,
                            reports: List[Dict[str, Any]], actor: str) -> Dict[str, Any]:
        """按批次号合并出发/到场/撤离回传：最早现场时间胜出，矛盾状态挂起待确认。"""
        from .rules import evaluate_phase
        self.get_zone(zone_id)
        self.get_resource(resource_id)
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM offline_batches WHERE batch_no=?", (batch_no,)
            ).fetchone()
            now = utc_now()
            if row is None:
                batch_id = int(self.conn.execute(
                    """INSERT INTO offline_batches(batch_no, zone_id, resource_id, phases,
                       pending, occupancy_id, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (batch_no, zone_id, resource_id, "{}", "[]", None, actor, now, now),
                ).lastrowid)
                phases: Dict[str, str] = {}
                pending: List[Dict[str, Any]] = []
                occupancy_id = None
            else:
                batch_id = row["id"]
                phases = json.loads(row["phases"])
                pending = json.loads(row["pending"])
                occupancy_id = row["occupancy_id"]
                if zone_id != row["zone_id"] or resource_id != row["resource_id"]:
                    raise ConflictError("批次号已绑定其他任务区或资源")
            phase_dts = {phase: self._dt(text) for phase, text in phases.items()}
            results = []
            for report in reports:
                phase = report["phase"]
                text = report["event_time"]
                dt = self._dt(text)
                verdict = evaluate_phase(phase, dt, phase_dts)
                entry = {"phase": phase, "event_time": text,
                         "uploaded_at": report.get("uploaded_at", now),
                         "uploaded_by": report.get("uploaded_by", actor)}
                if verdict == "accept":
                    phase_dts[phase] = dt
                    phases[phase] = dt.replace(microsecond=0).isoformat()
                    occupancy_id = self._reconcile_occupancy(
                        zone_id, resource_id, occupancy_id, phases, actor)
                    results.append({"phase": phase, "event_time": text, "result": "merged"})
                elif verdict == "duplicate":
                    results.append({"phase": phase, "event_time": text, "result": "duplicate"})
                else:
                    entry["status"] = "pending"
                    pending.append(entry)
                    results.append({"phase": phase, "event_time": text, "result": "conflict"})
            self.conn.execute(
                """UPDATE offline_batches SET phases=?, pending=?, occupancy_id=?,
                   updated_at=? WHERE id=?""",
                (json.dumps(phases, ensure_ascii=False, sort_keys=True),
                 json.dumps(pending, ensure_ascii=False, sort_keys=True),
                 occupancy_id, now, batch_id),
            )
        return self.get_offline_batch(batch_id)

    def resolve_offline_pending(self, batch_id: int, index: int, accepted: bool,
                                actor: str) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM offline_batches WHERE id=?", (batch_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("离线批次不存在")
            pending = json.loads(row["pending"])
            if index < 0 or index >= len(pending):
                raise NotFoundError("待确认记录不存在")
            entry = pending.pop(index)
            phases = json.loads(row["phases"])
            occupancy_id = row["occupancy_id"]
            if accepted:
                phases[entry["phase"]] = self._dt(entry["event_time"]).replace(microsecond=0).isoformat()
                occupancy_id = self._reconcile_occupancy(
                    row["zone_id"], row["resource_id"], occupancy_id, phases, actor)
            self.conn.execute(
                """UPDATE offline_batches SET phases=?, pending=?, occupancy_id=?,
                   updated_at=? WHERE id=?""",
                (json.dumps(phases, ensure_ascii=False, sort_keys=True),
                 json.dumps(pending, ensure_ascii=False, sort_keys=True),
                 occupancy_id, utc_now(), batch_id),
            )
        batch = self.get_offline_batch(batch_id)
        batch["resolved"] = {"index": index, "accepted": accepted, "entry": entry}
        return batch

    def close(self) -> None:
        with self._lock:
            self.conn.close()
