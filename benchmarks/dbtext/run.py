"""Run the existing DBText evaluator with a portable, bounded process launcher."""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import resource
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'src'))
from compression_lab import candidate, strings
from compression_lab.util import save, sha


def native_run(config, library, operation, source, output, capacity, ids=None):
    """Only launches the driver; C++ owns every reported operation timer.

    This mirrors the server's process launcher, where user namespaces were
    unavailable. It is intended for the reviewed codecs supplied here.
    """
    output = Path(output)
    output.mkdir()
    argv = [config['driver']['path'], str(library), operation, str(source),
            str(output / 'data'), str(capacity), str(ids) if ids else '/dev/null']

    def limits():
        os.sched_setaffinity(0, {config['cpu']})
        resource.setrlimit(resource.RLIMIT_AS, (config['memory_bytes'],) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (300, 301))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    env = dict(os.environ, LC_ALL='C', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    env['LD_LIBRARY_PATH'] = ':'.join(sorted({str(Path(p['path']).parent) for p in config['libraries'].values()}))
    result = subprocess.run(argv, capture_output=True, text=True, env=env, cwd=output,
                            preexec_fn=limits, timeout=config['timeout_seconds'])
    (output / 'stdout').write_text(result.stdout)
    (output / 'stderr').write_text(result.stderr)
    if result.returncode:
        raise RuntimeError(result.stderr)
    timing = json.loads(result.stdout)
    for key in ('setup_seconds', 'operation_seconds', 'warm_seconds', 'cleanup_seconds'):
        if not math.isfinite(timing[key]) or timing[key] < 0:
            raise RuntimeError('Invalid driver timing')
    if timing['operation_seconds'] <= 0 or timing['output_bytes'] != (output / 'data').stat().st_size:
        raise RuntimeError('Invalid driver output')
    return timing, {'resources': {'cpu': config['cpu'], 'filesystem_sandbox': False},
                    'note': 'Operation timing comes from the unchanged native driver.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cpu', type=int, default=min(os.sched_getaffinity(0)))
    parser.add_argument('--methods', nargs='+', default=['lz4', 'zstd1', 'fsst', 'onpairplus',
                                                       'astra-bulk', 'astra-fastencode', 'astra-rows'])
    parser.add_argument('--smoke', action='store_true', help='One supplied column, two passes; not a reported benchmark')
    args = parser.parse_args()
    build = args.build_dir.resolve()
    if args.smoke:
        columns = [(p.name, p) for p in sorted(args.data_dir.iterdir()) if p.is_file()][:1]
    else:
        columns = []
        for item in json.loads((HERE / 'columns.json').read_text()):
            path = args.data_dir / item['name']
            if path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
                raise RuntimeError('Wrong dataset column: ' + item['name'])
            columns.append((item['name'], path))
    libs = candidate.libraries()
    libs['libfsst.so'] = build / 'fsst-lib/libfsst.so'
    pins = {name: dict(path=str(libs[name]), sha256=sha(libs[name]), standard_codec=True)
            for name in ('libfsst.so', 'liblz4.so.1', 'libzstd.so.1')}
    strings.init(args.out, columns, build / 'driver', pins, args.cpu)
    strings._run = native_run
    results = []
    for method in args.methods:
        print('Measuring', method, flush=True)
        result = strings.evaluate(args.out, build / method / 'manifest.json', quick=args.smoke)
        if not result['quality_passed']:
            raise RuntimeError(json.dumps(result.get('error')))
        results.append(result)
    save(args.out / 'summary.json', {'smoke': args.smoke, 'results': results})


if __name__ == '__main__':
    main()
