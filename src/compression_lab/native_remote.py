"""Queue qualified native exports for the host-owned server benchmark worker."""
import fcntl
from pathlib import Path
import re
import time
import uuid

from . import native_strings as native
from .util import Error, load, save, sha


def status(root, job_id, include_columns=False):
    if not re.fullmatch('[0-9a-f]{32}', job_id):
        raise Error('server_invalid_job_id')
    folder = Path(root)/'server-jobs'/job_id
    state = load(folder/'state.json')
    if state['status'] == 'complete':
        if sha(folder/'comparison.json') != state['remote']['comparison_sha256']:
            raise Error('server_comparison_changed')
        comparison = load(folder/'comparison.json')
        if not include_columns:
            for row in comparison['results']:
                row.pop('columns', None)
        state['comparison'] = comparison
    return state


def submit(root, result_id):
    root = Path(root)
    if not (root/'server.json').exists():
        raise Error('server_benchmark_not_configured')
    row, _ = native.result(root, result_id)
    if len(row['columns']) != 23 or row['original_bytes'] != 39841347:
        raise Error('server_requires_complete_dbtext')
    jobs = root/'server-jobs'
    with (jobs/'submit.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        active = jobs/'active.json'
        if active.exists():
            previous = status(root, load(active)['job_id'])
            if previous['status'] in ('queued', 'running', 'connection_lost'):
                return {**previous, 'already_running': True}
        bundle = native.export(root, result_id)
        job = uuid.uuid4().hex
        folder = jobs/job
        folder.mkdir(mode=0o770)
        folder.chmod(0o2770)
        request = {'job_id': job, 'result_id': result_id, 'export_sha256': bundle['sha256'],
                   'variant': row['variant'], 'submitted_epoch': time.time()}
        save(folder/'request.json', request, 0o440)
        state = {**request, 'status': 'queued'}
        save(folder/'state.json', state, 0o660)
        save(active, {'job_id': job})
        return state
