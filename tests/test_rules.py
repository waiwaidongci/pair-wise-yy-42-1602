import unittest
from src import rules
from src.domain import ConflictError, ValidationError
from datetime import datetime, timezone


def dt(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


class RulesTest(unittest.TestCase):
    def test_priority_deadline_and_escalation(self):
        low=rules.priority_score(rules.SEVERITIES[0],1,10,0); high=rules.priority_score(rules.SEVERITIES[-1],30,10,3)
        self.assertGreater(high,low); self.assertLessEqual(rules.response_deadline_hours(rules.SEVERITIES[-1],30,10),rules.response_deadline_hours(rules.SEVERITIES[0],1,10))
        self.assertTrue(rules.escalation_required(rules.SEVERITIES[-1],1,10)); self.assertTrue(rules.escalation_required(rules.SEVERITIES[0],10,10))
    def test_transition_guards(self):
        self.assertTrue(rules.can_transition(rules.STATES[0],rules.STATES[1]))
        with self.assertRaises(ConflictError): rules.validate_transition(rules.STATES[0],rules.STATES[-1])
        with self.assertRaises(ValidationError): rules.priority_score("not-a-severity",1,1)
    def test_interval_overlap(self):
        t0,t1,t2,t3=(dt(f"2026-09-25T{h:02d}:00:00+00:00") for h in (10,11,12,13))
        # 闭合区间重叠
        self.assertTrue(rules.intervals_overlap(t0,t2,t1,t3))
        # 端点相接（班次交接）不算重叠
        self.assertFalse(rules.intervals_overlap(t0,t2,t2,t3))
        # 未撤离开放区间与后续时段重叠
        self.assertTrue(rules.intervals_overlap(t0,None,t2,t3))
        self.assertFalse(rules.intervals_overlap(t0,t1,t2,None))
    def test_ordering_conflicts(self):
        t0,t1,t2=(dt(f"2026-09-25T{h:02d}:00:00+00:00") for h in (8,9,10))
        self.assertEqual(rules.ordering_conflicts(
            {"departed":t0,"arrived":t1,"evacuated":t2}), [])
        # 到场早于出发 -> arrived矛盾
        self.assertEqual(rules.ordering_conflicts(
            {"departed":t1,"arrived":t0,"evacuated":t2}), ["arrived"])
        # 撤离早于到场 -> evacuated矛盾
        self.assertEqual(rules.ordering_conflicts(
            {"departed":t0,"arrived":t2,"evacuated":t1}), ["evacuated"])
    def test_zone_close_blockers(self):
        self.assertEqual(rules.zone_close_blockers(0,0), [])
        blockers=rules.zone_close_blockers(2,1)
        self.assertEqual(len(blockers),2)
        self.assertTrue(any("未撤离" in b for b in blockers))
        self.assertTrue(any("待确认" in b for b in blockers))
if __name__=="__main__": unittest.main()
