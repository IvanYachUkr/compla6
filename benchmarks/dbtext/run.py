"""Run the existing DBText evaluator with a portable, bounded process launcher."""
import argparse
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
from compression_lab.benchmark_run import write_metadata
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
        seconds = math.ceil(config['timeout_seconds'])
        resource.setrlimit(resource.RLIMIT_CPU, (seconds, seconds + 1))
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
    parser.add_argument('--columns', type=Path, default=HERE / 'columns.json',
                        help='Pinned [{name, bytes, sha256}] input manifest; default: complete DBText')
    parser.add_argument('--row-framing', choices=['lf', 'nul', 'none'], default='lf')
    parser.add_argument('--build-dir', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cpu', type=int, default=min(os.sched_getaffinity(0)))
    parser.add_argument('--methods', nargs='+', help='Defaults to methods in build-profile.json')
    parser.add_argument('--smoke', action='store_true', help='One supplied column, two passes; not a reported benchmark')
    args = parser.parse_args()
    build = args.build_dir.resolve()
    profile = json.loads((build / 'build-profile.json').read_text()) if (build / 'build-profile.json').exists() else {}
    if profile.get('row_framing', 'lf') != args.row_framing:
        raise RuntimeError('Build and evaluation row framing must match')
    methods = args.methods or profile.get('methods') or ['lz4', 'zstd1', 'fsst', 'onpairplus',
                                                        'astra-bulk', 'astra-fastencode', 'astra-rows']
    if len(set(methods)) != len(methods):
        raise RuntimeError('Methods must be distinct')
    if args.smoke:
        columns = [(p.name, p) for p in sorted(args.data_dir.iterdir()) if p.is_file()][:1]
    else:
        columns = []
        for item in json.loads(args.columns.read_text()):
            path = args.data_dir / item['name']
            if path.stat().st_size != item['bytes'] or sha(path) != item['sha256']:
                raise RuntimeError('Wrong dataset column: ' + item['name'])
            columns.append((item['name'], path))
    libs = candidate.libraries()
    libs['libfsst.so'] = build / 'fsst-lib/libfsst.so'
    binaries = []
    needed = set()
    for method in methods:
        manifest = json.loads((build / method / 'manifest.json').read_text())
        if args.row_framing == 'none' and manifest['variant'] != 'bulk':
            raise RuntimeError('Opaque-byte workloads support bulk methods only')
        for role in ('encoder', 'decoder'):
            library = build / method / manifest[role]; binaries.append(library)
            needed.update(set(candidate.elf(library)['needed']) - candidate.PLATFORM)
    if needed - {'libfsst.so', 'liblz4.so.1', 'libzstd.so.1'}:
        raise RuntimeError('Benchmark dependency is not a declared baseline codec: ' + ', '.join(sorted(needed)))
    pins = {name: dict(path=str(libs[name]), sha256=sha(libs[name]), standard_codec=True) for name in sorted(needed)}
    strings.init(args.out, columns, build / 'driver', pins, args.cpu, row_framing=args.row_framing,
                 driver_sources={'driver.cpp': HERE/'benchmark.cpp', 'codec.h': HERE/'codec.h'})
    config = strings.config(args.out)
    write_metadata(args.out, protocol='strings-v1', inputs=dict(columns), cpu=args.cpu, smoke=args.smoke,
        dataset='DBText' if args.columns.resolve() == (HERE/'columns.json').resolve() else args.columns.stem,
        parameters={**{key:config[key] for key in ('row_framing','warmups','seed','memory_bytes',
                                                 'timeout_seconds','selectivities','full_decode','row_decode')},
                    'trials':1 if args.smoke else config['trials']},
        artifacts=[build/'driver',*binaries,*(pin['path'] for pin in pins.values())])
    results = []
    for method in methods:
        print('Measuring', method, flush=True)
        result = strings.evaluate(args.out, build / method / 'manifest.json', quick=args.smoke, run=native_run)
        if not result['quality_passed']:
            raise RuntimeError(json.dumps(result.get('error')))
        results.append(result)
    save(args.out / 'summary.json', {'smoke': args.smoke, 'results': results})


if __name__ == '__main__':
    main()
