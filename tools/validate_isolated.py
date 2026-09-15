#!/usr/bin/env python3
"""Hold the host benchmark lease around a private-lock validation process tree.

Usage (run as vanya, never as root):
  .venv/bin/python tools/validate_isolated.py --output /new/output -- COMMAND ...

This is host validation infrastructure, not a production lock override. sudo and
mount/PID namespaces are required. The PID namespace has its own matching /proc.
The supervisor and namespace init retain the ORIGINAL lock descriptor until all
validation descendants are reaped. Engine/owner operations use a separate lock
bind-mounted at the same path inside this namespace. Timings are namespace-local
validation evidence, not claimed equivalent to live-campaign measurements.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import pwd
import signal
import stat
import subprocess
import sys
import time

GLOBAL_LOCK = Path('/run/compression-lab/benchmark.lock')
SCRIPT = Path(__file__).resolve()
STOP = None


def stamp():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def record(path, value, gid):
    """Write-once receipts; an existing path is always an error."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
    try:
        os.fchown(fd, 0, gid)
        with os.fdopen(fd, 'w', closefd=False) as output:
            json.dump(value, output, sort_keys=True, indent=2)
            output.write('\n'); output.flush(); os.fsync(fd)
    finally:
        os.close(fd)
    directory = os.open(Path(path).parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def identity(fd):
    info = os.fstat(fd)
    return {'device': info.st_dev, 'inode': info.st_ino, 'uid': info.st_uid,
            'mode': stat.S_IMODE(info.st_mode)}


def receive_stop(number, _frame):
    global STOP
    STOP = number


def reset_signals():
    for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(number, receive_stop)


def direct_children(parent_pid):
    """Use proc status: task/PID/children is unavailable on some host kernels."""
    children = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            parent = next(line for line in (entry / 'status').read_text().splitlines()
                          if line.startswith('PPid:'))
            if int(parent.split()[1]) == parent_pid:
                children.append(int(entry.name))
        except (OSError, StopIteration):
            continue
    return children


def reap_and_drain():
    """As namespace PID 1, adopt/reap every descendant, including setsid jobs."""
    require(os.getpid() == 1 and int(Path('/proc/self/stat').read_text().split(' ', 1)[0]) == 1,
            'cleanup_requires_matching_private_pid_namespace')
    started = time.monotonic(); signalled = set()
    while True:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break
        remaining = [int(path.name) for path in Path('/proc').iterdir()
                     if path.name.isdigit() and int(path.name) > 1]
        if not remaining:
            return {'complete': True, 'elapsed_seconds': time.monotonic() - started,
                    'descendants_signalled': len(signalled)}
        number = signal.SIGKILL if time.monotonic() - started >= 2 else signal.SIGTERM
        for pid in remaining:
            try:
                os.kill(pid, number); signalled.add(pid)
            except ProcessLookupError:
                pass
            except PermissionError:
                # The existing evaluator profile accepts signals only from peers.
                # This helper can target only an already enumerated private PID > 1.
                subprocess.run(['/usr/bin/aa-exec', '-p', 'compression-lab-evaluator', '--',
                                '/usr/bin/kill', '-s', str(int(number)), '--', str(pid)],
                               env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'},
                               stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, check=False)
                signalled.add(pid)
        # No timeout may release the global lease while a descendant still lives.
        time.sleep(0.05)


def namespace_main(output, lease_fd):
    require(os.geteuid() == 0 and os.getpid() == 1, 'namespace_init_required')
    reset_signals()
    run = json.loads((output / 'run.json').read_text())
    lease = json.loads((output / 'lease.json').read_text())
    require(hashlib.sha256(SCRIPT.read_bytes()).hexdigest() == run['launcher_sha256'],
            'launcher_source_changed_while_queued')
    require(identity(lease_fd) == lease['global_lock'], 'original_lease_descriptor_missing')
    require(os.readlink('/proc/self/ns/mnt') != run['host_mount_namespace'], 'mount_namespace_not_isolated')
    require(int(Path('/proc/self/stat').read_text().split(' ', 1)[0]) == 1, 'proc_mount_does_not_match_pid_namespace')
    subprocess.run(['/usr/bin/mount', '--bind', str(output / 'namespace.lock'), str(GLOBAL_LOCK)],
                   check=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'})
    shadow = os.open(GLOBAL_LOCK, os.O_RDWR | os.O_NOFOLLOW)
    try:
        private_identity = identity(shadow)
        require(private_identity['uid'] == 0 and private_identity['mode'] == 0o666, 'unsafe_namespace_lock')
        require(private_identity != identity(lease_fd), 'global_lock_was_not_shadowed')
        fcntl.flock(shadow, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(shadow, fcntl.LOCK_UN)
    finally:
        os.close(shadow)
    record(output / 'namespace.json', {'at': stamp(), 'pid': os.getpid(),
           'pid_namespace': os.readlink('/proc/self/ns/pid'),
           'mount_namespace': os.readlink('/proc/self/ns/mnt'),
           'proc_pid_matches': True, 'private_lock': private_identity,
           'original_lock_fd_retained': True}, run['gid'])

    def drop_identity():
        os.initgroups(run['user'], run['gid'])
        os.setgid(run['gid']); os.setuid(run['uid'])
        require(os.getuid() == run['uid'] and os.geteuid() == run['uid'], 'uid_drop_failed')

    started = time.monotonic(); child = None; code = 125; failure = None
    try:
        require(STOP is None, 'cancelled_before_validation_start')
        child = subprocess.Popen(run['command'], cwd=run['workspace'], env=run['environment'],
                                 preexec_fn=drop_identity, close_fds=True, start_new_session=True)
        while child.poll() is None:
            if STOP is not None:
                # Namespace-wide cleanup also catches descendants that created new sessions.
                break
            time.sleep(0.05)
        if STOP is None:
            code = child.returncode
        else:
            code = 128 + STOP
    except BaseException as error:
        failure = type(error).__name__ + ': ' + str(error)
    finally:
        cleanup = reap_and_drain()
        record(output / 'child-exit.json', {'at': stamp(), 'exit_code': code,
               'signal_received': STOP, 'error': failure, 'elapsed_seconds': time.monotonic() - started,
               'cleanup': cleanup, 'run_uid': run['uid']}, run['gid'])
    return code if 0 <= code <= 255 else 128 - code


def root_main(args):
    require(os.geteuid() == 0, 'sudo_root_supervisor_required')
    uid = int(os.environ.get('SUDO_UID', '-1')); account = pwd.getpwuid(uid)
    require(uid > 0 and account.pw_name == 'vanya', 'original_nonroot_vanya_identity_required')
    gid = account.pw_gid
    for tool in ('/usr/bin/unshare', '/usr/bin/mount', '/usr/bin/python3', '/usr/bin/flock',
                 '/usr/bin/aa-exec', '/usr/bin/kill'):
        require(Path(tool).is_file(), 'required_host_tool_missing:' + tool)
    workspace = Path(args.workspace).resolve(strict=True)
    output = Path(args.output).absolute()
    require(output.parent.resolve(strict=True) == output.parent, 'output_parent_must_be_canonical')
    require(workspace.is_dir(), 'workspace_required')
    require(args.command and Path(args.command[0]).is_absolute(), 'absolute_command_executable_required')
    require(Path(args.native_prefix).is_dir() and Path(args.rust_toolchain).is_dir(), 'pinned_toolchains_missing')
    # Fail before joining the benchmark queue if namespaces are unavailable.
    subprocess.run(['/usr/bin/unshare', '--mount', '--pid', '--fork', '--kill-child=KILL',
                    '--mount-proc', '--propagation', 'private', '--', '/usr/bin/true'],
                   check=True, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'})
    output.mkdir(mode=0o755)  # exclusive: never use exist_ok or replace old evidence
    os.chown(output, 0, gid); os.chmod(output, 0o755)
    work = output / 'work'; work.mkdir(mode=0o755); os.chown(work, uid, gid); os.chmod(work, 0o755)
    environment = {'HOME': account.pw_dir, 'USER': account.pw_name, 'LOGNAME': account.pw_name,
                   'PATH': str(workspace / '.venv/bin') + ':' + str(Path(args.rust_toolchain) / 'bin') + ':/usr/bin:/bin',
                   'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'TMPDIR': '/tmp',
                   'PYTHONPATH': str(workspace / 'src'), 'PYTHONUNBUFFERED': '1',
                   'COMPRESSION_LAB_NATIVE_PREFIX': str(Path(args.native_prefix).resolve(strict=True)),
                   'COMPRESSION_LAB_RUST_TOOLCHAIN': str(Path(args.rust_toolchain).resolve(strict=True))}
    run = {'schema_version': 1, 'created_at': stamp(), 'user': account.pw_name, 'uid': uid, 'gid': gid,
           'workspace': str(workspace), 'command': args.command, 'environment': environment,
           'launcher_sha256': hashlib.sha256(SCRIPT.read_bytes()).hexdigest(),
           'host_mount_namespace': os.readlink('/proc/self/ns/mnt'),
           'timing_scope': 'private validation mount/PID namespace; no equivalence claim to live campaign timings'}
    record(output / 'run.json', run, gid)
    lock_fd = os.open(GLOBAL_LOCK, os.O_RDWR | os.O_NOFOLLOW)
    require(identity(lock_fd)['uid'] == 0 and identity(lock_fd)['mode'] == 0o666, 'unsafe_global_lock')
    require(args.wait_seconds > 0, 'positive_lease_wait_timeout_required')
    reset_signals(); queued = time.monotonic(); waiter = None; child = None; acquired = None; code = 125; error = None
    print(json.dumps({'status': 'queued', 'output': str(output)}), flush=True)
    try:
        # flock locks this shared open file description. Our descriptor keeps
        # the lease after the waiter exits, and kernel queuing avoids poll starvation.
        waiter = subprocess.Popen(['/usr/bin/flock', '--exclusive', '--timeout', str(args.wait_seconds),
                                   '--conflict-exit-code', '75', str(lock_fd)],
                                  env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'},
                                  pass_fds=(lock_fd,), start_new_session=True)
        while waiter.poll() is None:
            if STOP is not None:
                waiter.terminate(); waiter.wait(); break
            time.sleep(0.1)
        if waiter.returncode == 0:
            acquired = time.monotonic()
        require(STOP is None, 'cancelled_while_queued')
        require(waiter.returncode != 75, 'global_lease_wait_timeout')
        require(waiter.returncode == 0, 'global_lease_wait_failed:' + str(waiter.returncode))
        # A second process in the OUTER namespace must fail to take the real lock.
        probe = subprocess.run(['/usr/bin/flock', '--nonblock', str(GLOBAL_LOCK), '/usr/bin/true'],
                               env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'}, close_fds=True)
        require(probe.returncode == 1, 'global_lease_exclusion_failed')
        record(output / 'lease.json', {'at': stamp(), 'queue_wait_seconds': acquired - queued,
               'global_lock': identity(lock_fd), 'outside_namespace_exclusion_verified': True}, gid)
        private_fd = os.open(output / 'namespace.lock', os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o666)
        try:
            os.fchmod(private_fd, 0o666); os.fchown(private_fd, 0, 0); os.fsync(private_fd)
        finally:
            os.close(private_fd)
        command = ['/usr/bin/unshare', '--mount', '--pid', '--fork', '--kill-child=KILL',
                   '--mount-proc', '--propagation', 'private', '--', '/usr/bin/python3', '-I',
                   str(SCRIPT), '--_namespace', str(output), str(lock_fd)]
        with (output / 'stdout.log').open('x') as stdout, (output / 'stderr.log').open('x') as stderr:
            os.chown(stdout.name, 0, gid); os.chmod(stdout.name, 0o640)
            os.chown(stderr.name, 0, gid); os.chmod(stderr.name, 0o640)
            child = subprocess.Popen(command, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8'},
                                     stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                     pass_fds=(lock_fd,), start_new_session=True)
            print(json.dumps({'status': 'running', 'output': str(output), 'supervisor_pid': os.getpid()}), flush=True)
            while child.poll() is None:
                if STOP is not None:
                    # Signal namespace init through its host PID; keep unshare and our lease alive.
                    # Repeat until exit, including the brief interval before its handler is installed.
                    for pid in direct_children(child.pid):
                        try:
                            os.kill(pid, STOP)
                        except ProcessLookupError:
                            pass
                time.sleep(0.1)
            code = child.returncode
        nested = json.loads((output / 'child-exit.json').read_text()) if (output / 'child-exit.json').exists() else None
        require(nested is not None and nested['cleanup']['complete'], 'namespace_cleanup_receipt_missing')
    except BaseException as failure:
        error = type(failure).__name__ + ': ' + str(failure)
        code = 125
    finally:
        if waiter is not None and waiter.poll() is None:
            waiter.terminate(); waiter.wait()
        # SIGKILL of this supervisor still leaves the inherited lease in namespace init.
        if child is not None and child.poll() is None:
            child.wait()  # keep the lease, including on an infrastructure exception
        receipt = {'schema_version': 1, 'completed_at': stamp(), 'exit_code': code, 'error': error,
                   'signal_received': STOP, 'global_lease_acquired': acquired is not None,
                   'queue_wait_seconds': (acquired or time.monotonic()) - queued,
                   'lease_seconds': time.monotonic() - acquired if acquired is not None else 0,
                   'command': args.command, 'uid': uid, 'output': str(output)}
        record(output / 'receipt.json', receipt, gid)
        os.close(lock_fd)
        print(json.dumps(receipt, sort_keys=True), flush=True)
    return code if 0 <= code <= 255 else 128 - code


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--_namespace':
        return namespace_main(Path(sys.argv[2]), int(sys.argv[3]))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', default=str(SCRIPT.parent.parent))
    parser.add_argument('--output', required=True)
    parser.add_argument('--native-prefix', default='/opt/compression-lab-native-2026-09-07')
    parser.add_argument('--rust-toolchain', default='/opt/compression-lab-native-2026-09-07/rust-1.95.0')
    parser.add_argument('--wait-seconds', type=float, default=600)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command[:1] == ['--']:
        args.command = args.command[1:]
    if os.geteuid() != 0:
        require(os.getuid() > 0 and pwd.getpwuid(os.getuid()).pw_name == 'vanya', 'run_as_vanya_required')
        os.execve('/usr/bin/sudo', ['sudo', '-n', '--', '/usr/bin/python3', '-I', str(SCRIPT), *sys.argv[1:]],
                  {'PATH': '/usr/bin:/bin', 'HOME': pwd.getpwuid(os.getuid()).pw_dir, 'LANG': 'C.UTF-8'})
    return root_main(args)


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as error:
        print(json.dumps({'status': 'blocked', 'error': type(error).__name__ + ': ' + str(error)}), file=sys.stderr)
        sys.exit(125)
