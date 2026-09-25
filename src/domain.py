from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message,details=None):
        super().__init__(message); self.message=message; self.details=details
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; ROLES=['field_commander', 'incident_commander', 'logistics', 'viewer']
# 任务区状态：active 可派单与接入资源，closed 关闭后只保留历史占用
ZONE_STATES=['active', 'closed']
# 离线回传事件类型：随班次出发 -> 到场 -> 撤离
OFFLINE_EVENTS=['departed', 'arrived', 'evacuated']
# 离线批次记录处理状态：confirmed 已并入占用，pending 存在矛盾待调度员确认，rejected 已驳回
REPORT_STATES=['confirmed', 'pending', 'rejected']
# 占用来源：dispatch 调度台派单/撤离，offline 离线批次同步
ASSIGNMENT_SOURCES=['dispatch', 'offline']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def parse_event_time(value, field="field_time"):
    """解析离线回传的现场时间（ISO 8601），统一成带UTC时区的datetime用于比较。"""
    value=require_text(value, field, 100)
    text=value.replace("Z", "+00:00")
    try:
        parsed=datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError(f"{field}必须是ISO 8601时间") from exc
    if parsed.tzinfo is None:
        parsed=parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
def to_event_time(dt):
    """datetime序列化为与audit一致的秒级ISO字符串。"""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()
