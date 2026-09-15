#!/usr/bin/env python3
"""Quarantine one proven inactive Prime 0.9.3 failed registration, retaining its sessions."""
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import pwd
import subprocess


def unit_state(unit):
    value = subprocess.check_output(
        ['systemctl', 'show', '--property=ActiveState,MainPID,ControlGroup', '--', unit], text=True)
    return dict(line.split('=', 1) for line in value.splitlines() if '=' in line)


def require_stopped(campaign):
    state = unit_state(campaign['driver_unit'])
    if state.get('ActiveState') not in ('inactive', 'failed') or state.get('MainPID') != '0':
        raise RuntimeError('The exact campaign driver must be inactive before recovery')
    group = state.get('ControlGroup')
    if group:
        procs = Path('/sys/fs/cgroup')/group.lstrip('/')/'cgroup.procs'
        if not procs.is_file() or procs.read_text().strip():
            raise RuntimeError('The campaign control group is populated or unreadable')
    uid = int(campaign['agent_uid'])
    if uid <= 0 or (campaign.get('agent_user') and pwd.getpwnam(campaign['agent_user']).pw_uid != uid):
        raise RuntimeError('A matching dedicated nonroot native agent UID is required')
    result = subprocess.run(['pgrep', '-u', str(uid)], capture_output=True)
    if result.returncode != 1:
        raise RuntimeError('Native agent processes remain, or process absence could not be proved')


def recover(campaign, output, apply=False):
    require_stopped(campaign)
    parent = campaign['native_session']
    profile = Path(campaign['profile']).resolve(strict=True)
    registry = profile/'.prime/agent/daemon-workers'
    if not registry.resolve(strict=True).is_relative_to(profile):
        raise RuntimeError('Worker registry escapes the owned profile')
    matches = []
    for path in registry.glob('*/*.json'):
        if path.is_symlink() or not path.resolve(strict=True).is_relative_to(profile):
            raise RuntimeError('Worker descriptor is a symlink or escapes the owned profile')
        raw = path.read_bytes()
        descriptor = json.loads(raw)
        if (descriptor.get('rootSessionId') == parent['session_id'] and
                descriptor.get('sessionFile') == parent['session_file']):
            matches.append((path, raw, descriptor))
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one descriptor for the verified root and saved session')
    path, raw, descriptor = matches[0]
    if (descriptor.get('lifecycle') != 'failed' or not descriptor.get('ownerClientId') or
            descriptor.get('stopRequestedAt') is not None):
        raise RuntimeError('Descriptor is not the known failed client-owned registration case')
    output = Path(output).absolute()
    if output.resolve().is_relative_to(profile) or output.exists() or output.is_symlink():
        raise RuntimeError('Use a new private recovery directory outside the agent profile')
    receipt = {
        'at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
        'status': 'verified_inactive' if not apply else 'quarantined',
        'source': str(path), 'quarantined_to': str(output/path.name),
        'descriptor_sha256': hashlib.sha256(raw).hexdigest(),
        'root_session_id': parent['session_id'], 'session_file': parent['session_file'],
        'native_uid_has_no_processes': True,
        'scope': 'One failed registration; native transcripts, leases, spawn ledger and installed packages retained',
    }
    if apply:
        # Recheck immediately before the only mutation. Never recover a live
        # native tree or choose among ambiguous registrations.
        require_stopped(campaign)
        if path.read_bytes() != raw:
            raise RuntimeError('Descriptor changed after the inactive snapshot')
        output.mkdir(parents=True, mode=0o700, exist_ok=False)
        output.chmod(0o700)
        target = output/path.name
        path.rename(target)
        target.chmod(0o600)
        proof = output/'RECOVERY.json'
        proof.write_text(json.dumps(receipt, indent=2)+'\n')
        proof.chmod(0o600)
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--campaign', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    stat = args.campaign.stat()
    if (not args.campaign.is_file() or args.campaign.is_symlink() or
            stat.st_uid != os.geteuid() or stat.st_mode & 0o022):
        parser.error('Campaign must be a caller-owned regular file protected from other writers')
    print(json.dumps(recover(json.loads(args.campaign.read_text()), args.output, args.apply), indent=2))


if __name__ == '__main__':
    main()
