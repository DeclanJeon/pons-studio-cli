"""Read-only operational projection, never a content/QA authority.

Percent is the reported workflow position, not elapsed time or provider completion.
Only independently accepted delivery states can reach 100 percent.
"""
from datetime import datetime, timezone
from statistics import median

STAGES = ['intake', 'story', 'characters', 'voice', 'storyboard', 'images', 'render', 'review', 'archive']
ACTIVE = {'running', 'cancelling', 'archiving'}
STOPPED = {'failed', 'interrupted', 'cancelled', 'archive_failed'}


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def elapsed(job, current):
    start = timestamp(job.get('started_at'))
    end = current if job['status'] in ACTIVE else timestamp(job.get('updated_at'))
    return max(0, int((end - start).total_seconds())) if start and end else 0


def signature(job):
    request = job['request']
    return (request.get('target_duration_s'), request.get('aspect_ratio'),
            bool(request.get('drive')), sorted(request.get('connections', {}).items()))


def snapshot(job, latest=None, histories=(), current=None):
    current = current or datetime.now(timezone.utc)
    stages = STAGES if job['request'].get('drive') else STAGES[:-1]
    stage = job['stage']
    index = stages.index(stage) if stage in stages else len(stages) - 1 if stage == 'archive' else 0
    delivered = bool(job.get('result', {}).get('video')) if job.get('result') else False
    complete = delivered and (job['status'] == 'completed' or
                             job['status'] == 'animatic_ready' and not job['request'].get('drive'))
    # Reporting "review" never implies that the independent delivery gate passed.
    done = len(stages) if complete else index
    percent = 100 if complete else int(100 * done / len(stages))
    duration = elapsed(job, current)
    samples = []
    for other in histories:
        if other['id'] == job['id'] or signature(other) != signature(job):
            continue
        if other.get('attempt') != 1 or not (other.get('result') or {}).get('video'):
            continue
        if other['status'] != ('completed' if other['request'].get('drive') else 'animatic_ready'):
            continue
        measured = elapsed(other, current)
        if measured > 0:
            samples.append(measured)
    eta = None
    eta_state = 'collecting'
    if job['status'] in STOPPED:
        eta_state = 'stopped'
    elif complete:
        eta_state = 'complete'
    elif job['status'] == 'queued':
        eta_state = 'queued'
    elif job.get('attempt', 0) > 1:
        eta_state = 'resumed'
    elif len(samples) >= 3 and job['status'] in ACTIVE:
        remaining = int(median(samples)) - duration
        eta_state = 'estimated' if remaining > 0 else 'overdue'
        if remaining > 0:
            eta = remaining
    last_at = latest['at'] if latest else job.get('updated_at')
    last_time = timestamp(last_at)
    return {'percent': percent, 'basis': 'reported_stage', 'stage': stage,
            'stage_number': index + 1, 'stage_count': len(stages),
            'completed_stages': stages[:done], 'complete': complete,
            'elapsed_s': duration, 'clock_running': job['status'] in ACTIVE,
            'as_of': current.isoformat(), 'last_activity_at': last_at,
            'quiet_s': max(0, int((current-last_time).total_seconds())) if last_time else 0,
            'eta_s': eta, 'eta_state': eta_state, 'eta_samples': len(samples),
            'latest_message': latest['message'][:240] if latest else None}
