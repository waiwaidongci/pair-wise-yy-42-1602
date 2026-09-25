from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .domain import (ConflictError, DomainError, NotFoundError, PermissionDenied,
                     ValidationError)
from .service import Service


def make_handler(service: Service, static_dir: str):
    root = Path(static_dir)

    class Handler(BaseHTTPRequestHandler):
        server_version = "ModularHell/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            return

        def _json(self, status: int, payload: Any) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _html(self, path: Path) -> None:
            if not path.exists():
                self._json(404, {"error": "not_found"})
                return
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _identity(self) -> Tuple[str, str]:
            return self.headers.get("X-Actor", ""), self.headers.get("X-Role", "")

        def _body(self) -> Dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0") or 0)
            if length <= 0:
                return {}
            if length > 2_000_000:
                raise ValidationError("请求体过大")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValidationError("请求体不是有效JSON") from exc
            if not isinstance(value, dict):
                raise ValidationError("请求体必须是JSON对象")
            return value

        def _send_error(self, exc: Exception) -> None:
            if isinstance(exc, ValidationError):
                status = 422
            elif isinstance(exc, NotFoundError):
                status = 404
            elif isinstance(exc, PermissionDenied):
                status = 403
            elif isinstance(exc, ConflictError):
                status = 409
            elif isinstance(exc, ValueError):
                status = 422
            elif isinstance(exc, DomainError):
                status = 400
            else:
                status = 500
            self._json(status, {"error": exc.__class__.__name__, "message": str(exc),
                                **({"details": exc.details} if getattr(exc, "details", None) else {})})

        def do_GET(self) -> None:
            try:
                path = urlparse(self.path).path
                if path == "/health":
                    self._json(200, {"status": "ok"})
                elif path == "/":
                    self._html(root / "index.html")
                elif path == "/api/items":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"items": service.list_items(role)})
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"records": service.list_records(item_id, role)})
                elif path.startswith("/api/items/"):
                    item_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_item(item_id, role))
                elif path == "/api/audit":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"events": service.audit(role)})
                elif path == "/api/zones":
                    actor, role = self._identity()
                    status = parse_qs(urlparse(self.path).query).get("status", [None])[0]
                    del actor
                    self._json(200, {"zones": service.list_zones(role, status)})
                elif path.startswith("/api/zones/") and path.endswith("/assignments"):
                    zone_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"assignments": service.list_zone_assignments(zone_id, role)})
                elif path.startswith("/api/zones/"):
                    zone_id = int(path.rsplit("/", 1)[-1])
                    actor, role = self._identity()
                    del actor
                    self._json(200, service.get_zone(zone_id, role))
                elif path == "/api/resources":
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"resources": service.list_resources(role)})
                elif path.startswith("/api/resources/") and path.endswith("/assignments"):
                    resource_id = int(path.split("/")[3])
                    actor, role = self._identity()
                    del actor
                    self._json(200, {"assignments": service.list_resource_assignments(resource_id, role)})
                elif path == "/api/offline-reports":
                    actor, role = self._identity()
                    query = parse_qs(urlparse(self.path).query)
                    review_status = query.get("status", [None])[0]
                    zone_value = query.get("zone_id", [None])[0]
                    zone_id = int(zone_value) if zone_value is not None else None
                    del actor
                    self._json(200, {"reports": service.list_offline_reports(role, review_status, zone_id)})
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

        def do_POST(self) -> None:
            try:
                path = urlparse(self.path).path
                actor, role = self._identity()
                body = self._body()
                if path == "/api/items":
                    self._json(201, service.create_item(body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/records"):
                    item_id = int(path.split("/")[3])
                    self._json(201, service.add_record(item_id, body, actor, role))
                elif path.startswith("/api/items/") and path.endswith("/transition"):
                    item_id = int(path.split("/")[3])
                    target = body.get("target")
                    expected = body.get("expected_version")
                    self._json(200, service.transition(
                        item_id, target, expected, actor, role))
                elif path == "/api/zones":
                    self._json(201, service.create_zone(body, actor, role))
                elif path.startswith("/api/zones/") and path.endswith("/close"):
                    zone_id = int(path.split("/")[3])
                    self._json(200, service.close_zone(zone_id, body, actor, role))
                elif path == "/api/resources":
                    self._json(201, service.create_resource(body, actor, role))
                elif path == "/api/assignments":
                    self._json(201, service.dispatch(body, actor, role))
                elif path == "/api/assignments/release":
                    self._json(200, service.release(body, actor, role))
                elif path == "/api/offline-reports":
                    self._json(201, service.upload_offline_batch(body, actor, role))
                elif path.startswith("/api/offline-reports/") and path.endswith("/review"):
                    report_id = int(path.split("/")[3])
                    self._json(200, service.review_offline_report(report_id, body, actor, role))
                else:
                    self._json(404, {"error": "not_found"})
            except Exception as exc:
                self._send_error(exc)

    return Handler
