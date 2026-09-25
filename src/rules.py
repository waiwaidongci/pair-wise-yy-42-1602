from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
ZONE_ENTITY='任务区'; RESOURCE_ENTITY='资源'; ASSIGNMENT_ENTITY='资源占用'; REPORT_ENTITY='离线批次记录'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
# 任务区/资源：现场或调度建立；派单（含离线确认）由调度员负责，离线回传由现场与后勤补录
ZONE_CREATE_ROLES=set(['field_commander', 'incident_commander']); RESOURCE_ROLES=set(['field_commander', 'incident_commander', 'logistics'])
DISPATCH_ROLES=set(['incident_commander', 'logistics']); OFFLINE_UPLOAD_ROLES=set(['field_commander', 'logistics']); REPORT_REVIEW_ROLES=set(['incident_commander'])
# 仍在场（未撤离）：到场有时间但撤离时间为空的占用开放区间
OPEN_ENDED=None
SEVERITY_WEIGHT={'low': 1.0, 'moderate': 3.0, 'high': 6.0, 'extreme': 9.0}; DEADLINE_HOURS={'low': 72, 'moderate': 24, 'high': 8, 'extreme': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def intervals_overlap(start_a,end_a,start_b,end_b):
    """两个半开时段是否重叠；end为None表示仍在场（开放区间）。端点相接不算重叠，允许班次交接。"""
    if start_b < start_a:
        start_a,end_a,start_b,end_b=start_b,end_b,start_a,end_a
    return end_a is None or start_b < end_a
def ordering_conflicts(times):
    """检查出发<=到场<=撤离的时序；times为event->datetime的映射，返回矛盾事件列表。"""
    conflicts=[]
    departed=times.get('departed'); arrived=times.get('arrived'); evacuated=times.get('evacuated')
    if departed is not None and arrived is not None and arrived < departed: conflicts.append('arrived')
    if arrived is not None and evacuated is not None and evacuated < arrived: conflicts.append('evacuated')
    if departed is not None and evacuated is not None and evacuated < departed:
        if 'evacuated' not in conflicts: conflicts.append('evacuated')
    return conflicts
def zone_close_blockers(open_assignments,pending_reports):
    """任务区关闭前，未撤离资源和未确认（待调度员确认）记录都不能被忽略。"""
    blockers=[]
    if open_assignments:
        blockers.append(f"仍有{open_assignments}个资源未撤离")
    if pending_reports:
        blockers.append(f"仍有{pending_reports}条离线记录待确认")
    return blockers
def earliest(values):
    """重复上传按最早的现场时间合并。"""
    present=[v for v in values if v is not None]
    return min(present) if present else None
