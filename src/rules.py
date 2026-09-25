from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='山火事件指挥与离线人员调度'; ENTITY='山火事件'; ID_PREFIX='WF'
SEVERITIES=['low', 'moderate', 'high', 'extreme']; STATES=['reported', 'active', 'contained', 'controlled', 'closed']; TRANSITIONS={'reported': ['active'], 'active': ['contained'], 'contained': ['controlled'], 'controlled': ['closed'], 'closed': []}; TRANSITION_ROLES={'active': ['incident_commander'], 'contained': ['incident_commander'], 'controlled': ['incident_commander'], 'closed': ['incident_commander']}
CREATE_ROLES=set(['field_commander']); RECORD_ROLES=set(['field_commander', 'logistics']); AUDIT_ROLES=set(['incident_commander', 'viewer']); VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])
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

# ---- 任务区与资源占用 ----
ZONE_STATES=['active', 'closed']
ZONE_TRANSITIONS={'active': ['closed'], 'closed': []}
ZONE_TRANSITION_ROLES={'closed': ['incident_commander']}
RESOURCE_KINDS=['team', 'vehicle']
# 离线回传阶段：出发 -> 到场 -> 撤离
OFFLINE_PHASES=['departed', 'arrived', 'withdrawn']
ZONE_CREATE_ROLES=set(['field_commander'])
RESOURCE_CREATE_ROLES=set(['logistics'])
ASSIGN_ROLES=set(['logistics'])
OFFLINE_UPLOAD_ROLES=set(['field_commander', 'logistics'])
OFFLINE_CONFIRM_ROLES=set(['field_commander', 'incident_commander', 'logistics'])
ZONE_VIEW_ROLES=set(['field_commander', 'incident_commander', 'logistics', 'viewer'])

def validate_zone_transition(current,target):
    if current not in ZONE_STATES or target not in ZONE_STATES: raise ValidationError("未知任务区状态")
    if target not in ZONE_TRANSITIONS.get(current,[]): raise ConflictError(f"任务区不能从{current}转换到{target}")
def role_for_zone_transition(target): return set(ZONE_TRANSITION_ROLES.get(target,[]))
def zone_closure_blockers(active_occupancies,pending_offline):
    blockers=[]
    if active_occupancies>0: blockers.append(f"仍有{active_occupancies}个资源未撤离")
    if pending_offline>0: blockers.append(f"仍有{pending_offline}条离线记录未确认")
    return blockers
def _phase_index(phase):
    if phase not in OFFLINE_PHASES: raise ValidationError("status必须是departed/arrived/withdrawn")
    return OFFLINE_PHASES.index(phase)
def evaluate_phase(phase,timestamp,current):
    """根据“最早现场时间合并”规则评估一条离线记录。
    current: {阶段: 最早时间(dict)}，返回 accept / duplicate / conflict。"""
    idx=_phase_index(phase)
    existing=current.get(phase)
    if existing is not None:
        return "duplicate" if timestamp>=existing else "conflict"
    for other,other_time in current.items():
        other_idx=OFFLINE_PHASES.index(other)
        if other_idx<idx and other_time>timestamp: return "conflict"
        if other_idx>idx and other_time<timestamp: return "conflict"
    return "accept"
