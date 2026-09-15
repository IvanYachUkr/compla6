"""One durable USD cap for every request in the three commissioned experiments.

Reservations include the most expensive input billing category and all permitted
output (including reasoning). Unknown requests retain their full reservation.
Published Standard prices checked 2026-09-12 at:
https://developers.openai.com/api/docs/pricing
"""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import tempfile
import time

CAP_NANO = 50_000_000_000
CONTEXT = 1_050_000
MAX_OUTPUT = 128_000
LONG_THRESHOLD = 272_000
RATES = {
    # Nanodollars per token: uncached input, cache read, cache write, output.
    'gpt-5.6-luna': ((200, 20, 250, 1200), (400, 40, 500, 1800)),
    'gpt-5.6-terra': ((2000, 200, 2500, 12000), (4000, 400, 5000, 18000)),
    'gpt-5.6-sol': ((4000, 400, 5000, 20000), (8000, 800, 10000, 30000)),
}


class Stopped(RuntimeError):
    pass


def atomic(path, value):
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.' + path.name + '-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


class Budget:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def locked(self):
        with self.path.with_suffix('.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            yield

    def create(self, cap=CAP_NANO):
        if type(cap) is not int or not 0 < cap <= CAP_NANO:
            raise Stopped('invalid_cap')
        with self.locked():
            if self.path.exists():
                raise Stopped('ledger_already_exists')
            atomic(self.path, {'version': 1, 'cap_nano': cap, 'stopped': None,
                               'requests': [], 'created_epoch': time.time()})

    def load(self):
        try:
            s = json.loads(self.path.read_text())
            assert s['version'] == 1 and type(s['cap_nano']) is int
            assert 0 < s['cap_nano'] <= CAP_NANO
            assert isinstance(s['requests'], list)
            for i, r in enumerate(s['requests'], 1):
                assert r['ticket'] == i and r['model'] in RATES
                assert r['status'] in ('pending', 'settled', 'uncertain', 'violation', 'capacity_hold')
                if r['status'] == 'capacity_hold': assert r.get('capacity_code') in CAPACITY_CODES
                assert type(r['reserved_nano']) is int and r['reserved_nano'] > 0
                if r['status'] == 'settled':
                    assert type(r['charged_nano']) is int
                    assert 0 <= r['charged_nano'] <= r['reserved_nano']
            return s
        except (OSError, ValueError, KeyError, TypeError, AssertionError) as exc:
            raise Stopped('ledger_missing_or_invalid') from exc

    @staticmethod
    def committed(s):
        return sum(r['charged_nano'] if r['status'] == 'settled'
                   else r['reserved_nano'] for r in s['requests'])

    def snapshot(self):
        with self.locked():
            s = self.load()
            s['committed_and_reserved_nano'] = self.committed(s)
            s['remaining_nano'] = max(0, s['cap_nano'] - self.committed(s))
            return s

    def stop(self, reason):
        with self.locked():
            s = self.load()
            s['stopped'] = s['stopped'] or reason
            atomic(self.path, s)

    def recover_infrastructure(self, expected_stop, review_id):
        """Owner-reviewed native failure; retain every charge and reservation."""
        recoverable = {'native_native_state_error', 'owned_research_service_stopped',
                       'native_session_identity_or_model_mismatch'}
        if expected_stop not in recoverable:
            raise Stopped('nonrecoverable_stop')
        if not isinstance(review_id, str) or not 1 <= len(review_id) <= 128:
            raise Stopped('invalid_review_id')
        with self.locked():
            s = self.load()
            if s['stopped'] != expected_stop:
                raise Stopped('stop_reason_changed')
            if any(r['status'] in ('pending', 'uncertain', 'violation') for r in s['requests']):
                raise Stopped('unresolved_request')
            committed = self.committed(s)
            if committed >= s['cap_nano']:
                raise Stopped('budget_cannot_cover_next_request')
            s.setdefault('recoveries', []).append({'review_id': review_id, 'stop_reason': expected_stop,
                'recovered_epoch': time.time(), 'committed_and_reserved_nano': committed})
            s['stopped'] = None
            atomic(self.path, s)

    def reserve(self, run, session, model, input_bound, max_output, payload_sha):
        if model not in RATES or any(type(n) is not int for n in (input_bound, max_output)):
            raise Stopped('invalid_request_bound')
        if not 0 < input_bound <= CONTEXT or not 0 < max_output <= MAX_OUTPUT:
            raise Stopped('invalid_request_bound')
        rates = RATES[model][input_bound > LONG_THRESHOLD]
        reserved = input_bound * rates[2] + max_output * rates[3]
        with self.locked():
            s = self.load()
            if s['stopped']:
                raise Stopped(s['stopped'])
            if any(r['status'] in ('uncertain', 'violation') for r in s['requests']):
                raise Stopped('unresolved_request')
            if self.committed(s) + reserved > s['cap_nano']:
                s['stopped'] = 'budget_cannot_cover_next_request'
                atomic(self.path, s)
                raise Stopped(s['stopped'])
            ticket = len(s['requests']) + 1
            s['requests'].append(dict(ticket=ticket, run=run, session=session, model=model,
                status='pending', reserved_nano=reserved, input_bound=input_bound,
                max_output=max_output, rates=list(rates), payload_sha256=payload_sha,
                started_epoch=time.time()))
            atomic(self.path, s)
            return ticket

    def settle(self, ticket, usage, *, response_id, response_model, service_tier):
        with self.locked():
            s = self.load()
            if type(ticket) is not int or not 1 <= ticket <= len(s['requests']):
                raise Stopped('unknown_ticket')
            r = s['requests'][ticket - 1]
            if r['status'] != 'pending':
                raise Stopped('request_already_closed')
            try:
                ni, no = usage['input_tokens'], usage['output_tokens']
                details = usage.get('input_tokens_details', {})
                cached = details.get('cached_tokens', 0)
                writes = details.get('cache_write_tokens')
                assert all(type(n) is int and n >= 0 for n in (ni, no, cached))
                assert cached <= ni
                assert writes is None or (type(writes) is int and 0 <= writes <= ni - cached)
                # Missing write accounting is charged at the maximum input rate.
                writes = ni - cached if writes is None else writes
                rates = RATES[r['model']][ni > LONG_THRESHOLD]
                charge = (ni - cached - writes) * rates[0] + cached * rates[1] + writes * rates[2] + no * rates[3]
                assert ni <= r['input_bound'] and no <= r['max_output']
                assert charge <= r['reserved_nano']
                assert service_tier in ('default', None)
                assert response_model == r['model'] or response_model.startswith(r['model'] + '-')
            except (AssertionError, KeyError, TypeError, AttributeError):
                r.update(status='violation', finished_epoch=time.time())
                s['stopped'] = 'usage_or_model_verification_failed'
                atomic(self.path, s)
                raise Stopped(s['stopped'])
            r.update(status='settled', charged_nano=charge, usage=usage,
                     response_id=response_id, response_model=response_model,
                     service_tier=service_tier, finished_epoch=time.time())
            atomic(self.path, s)

    def uncertain(self, ticket, reason):
        with self.locked():
            s = self.load()
            r = s['requests'][ticket - 1]
            if r['status'] == 'pending':
                r.update(status='uncertain', reason=reason, finished_epoch=time.time())
            s['stopped'] = s['stopped'] or reason
            atomic(self.path, s)

    def capacity_failure(self, ticket, code):
        """Retain the full unknown charge while allowing a separately funded retry."""
        if code not in CAPACITY_CODES: raise Stopped('unclassified_capacity_failure')
        with self.locked():
            s = self.load(); r = s['requests'][ticket-1]
            if r['status'] == 'pending':
                r.update(status='capacity_hold', capacity_code=code, finished_epoch=time.time())
            elif r['status'] != 'settled': raise Stopped('request_already_closed')
            atomic(self.path, s)


CAPACITY_CODES = {'server_is_overloaded', 'engine_overloaded', 'service_unavailable_error'}
