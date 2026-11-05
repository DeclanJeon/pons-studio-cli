import copy
import unittest
from datetime import datetime, timezone
from server.progress import snapshot

NOW = datetime(2026, 11, 3, 3, 0, tzinfo=timezone.utc)

def job(**overrides):
    return dict(id='prj_test', status='running', stage='voice', attempt=1,
                created_at='2026-11-03T02:00:00+00:00',
                started_at='2026-11-03T02:50:00+00:00',
                updated_at='2026-11-03T02:55:00+00:00',
                result=None, request={'drive':False, 'target_duration_s':60,
                                      'aspect_ratio':'16:9', 'connections':{'speech':'conn_a'}},
                **{}) | overrides

class ProgressTests(unittest.TestCase):
    def test_clock_does_not_drive_stage_percent(self):
        a=snapshot(job(),current=NOW)
        b=snapshot(job(started_at='2026-11-03T01:00:00+00:00'),current=NOW)
        self.assertEqual(a['percent'],37)
        self.assertEqual(a['percent'],b['percent'])
        self.assertEqual(a['stage_count'],8)
        self.assertEqual(a['elapsed_s'],600)

    def test_failure_freezes_clock_and_keeps_position(self):
        p=snapshot(job(status='failed'),current=NOW)
        self.assertEqual(p['elapsed_s'],300)
        self.assertEqual(p['percent'],37)
        self.assertFalse(p['clock_running'])
        self.assertIsNone(p['eta_s'])
        self.assertEqual(p['eta_state'],'stopped')

    def test_reporting_final_stage_cannot_claim_delivery(self):
        p=snapshot(job(stage='review'),current=NOW)
        self.assertLess(p['percent'],100)
        self.assertFalse(p['complete'])
        self.assertNotIn('review',p['completed_stages'])
        p=snapshot(job(status='completed',stage='archive'),current=NOW)
        self.assertLess(p['percent'],100)

    def test_local_delivery_reaches_100(self):
        p=snapshot(job(status='animatic_ready',stage='review',result={'video':'final.mp4'}),current=NOW)
        self.assertEqual(p['percent'],100)
        self.assertEqual(len(p['completed_stages']),8)

    def test_selected_archive_is_required_for_100(self):
        j=job(status='animatic_ready',stage='review',result={'video':'final.mp4'})
        j['request']['drive']=True
        self.assertLess(snapshot(j,current=NOW)['percent'],100)
        j.update(stage='archive',status='archive_failed')
        self.assertEqual(snapshot(j,current=NOW)['percent'],88)
        j['status']='completed'
        self.assertEqual(snapshot(j,current=NOW)['percent'],100)

    def test_eta_requires_three_comparable_first_attempt_completions(self):
        history=[]
        for i in range(3):
            history.append(job(id=f'prj_old{i}',status='animatic_ready',stage='review',
                               result={'video':'final.mp4'},updated_at='2026-11-03T03:10:00+00:00'))
        self.assertIsNone(snapshot(job(),histories=history[:2],current=NOW)['eta_s'])
        p=snapshot(job(),histories=history,current=NOW)
        self.assertEqual(p['eta_s'],600)
        self.assertEqual(p['eta_samples'],3)
        changed=copy.deepcopy(history);changed[0]['request']['target_duration_s']=180
        self.assertIsNone(snapshot(job(),histories=changed,current=NOW)['eta_s'])
        self.assertEqual(snapshot(job(attempt=2),histories=history,current=NOW)['eta_state'],'resumed')
        history[0]['attempt']=2
        self.assertIsNone(snapshot(job(),histories=history,current=NOW)['eta_s'])

    def test_overdue_is_not_a_false_zero_eta(self):
        history=[job(id=f'prj_old{i}',status='animatic_ready',result={'video':'f.mp4'}) for i in range(3)]
        p=snapshot(job(),histories=history,current=NOW)
        self.assertEqual(p['eta_state'],'overdue')
        self.assertIsNone(p['eta_s'])

    def test_queued_and_regression_do_not_keep_false_completed_ticks(self):
        p=snapshot(job(status='queued',started_at=None,stage='intake'),current=NOW)
        self.assertEqual(p['percent'],0)
        self.assertEqual(p['elapsed_s'],0)
        self.assertEqual(p['eta_state'],'queued')
        p=snapshot(job(stage='characters'),current=NOW)
        self.assertNotIn('voice',p['completed_stages'])

    def test_latest_activity_is_separate_from_project_metadata(self):
        p=snapshot(job(),{'at':'2026-11-03T02:59:30+00:00','message':'실제 이미지 응답을 기다립니다.'},current=NOW)
        self.assertEqual(p['quiet_s'],30)
        self.assertEqual(p['latest_message'],'실제 이미지 응답을 기다립니다.')

    def test_missing_or_future_clock_is_safe(self):
        self.assertEqual(snapshot(job(started_at=None),current=NOW)['elapsed_s'],0)
        self.assertEqual(snapshot(job(started_at='2026-11-04T00:00:00+00:00'),current=NOW)['elapsed_s'],0)
