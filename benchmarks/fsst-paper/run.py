"""Replay the unmodified FSST paper selection/timing loop with our ABI adapter."""
import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import hashlib

HERE = Path(__file__).resolve().parent
FSST = HERE.parent / 'upstream/fsst'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--build-dir', type=Path, required=True, help='Output from dbtext/build.py')
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cpu', type=int, default=min(os.sched_getaffinity(0)))
    parser.add_argument('--smoke', action='store_true', help='Use one supplied column and one replay')
    args = parser.parse_args()
    build, out = args.build_dir.resolve(), args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    if args.smoke:
        inputs = sorted(p.resolve() for p in args.data_dir.iterdir() if p.is_file())[:1]
    else:
        pins = json.loads((HERE.parent / 'dbtext/columns.json').read_text())
        inputs = []
        for col in pins:
            path = (args.data_dir / col['name']).resolve()
            if hashlib.sha256(path.read_bytes()).hexdigest() != col['sha256']:
                raise RuntimeError('Wrong input column: ' + col['name'])
            inputs.append(path)
        inputs.sort()
    if not inputs or any(p.stat().st_size >= 7000000 or not p.read_bytes().endswith(b'\n') for p in inputs):
        raise RuntimeError('Paper selective path requires LF-terminated columns below its 7 MB cutoff')
    argv = ['g++', '-std=c++17', '-O3', '-DNDEBUG', '-I' + str(FSST / 'paper'), '-I' + str(FSST),
            str(HERE / 'wrapper.cpp'), '-L' + str(build / 'fsst-lib'), '-lfsst', '-llz4', '-ldl',
            '-Wl,-rpath,' + str(build / 'fsst-lib'), '-o', str(out / 'filtertest')]
    result = subprocess.run(argv, capture_output=True, text=True, timeout=120)
    (out / 'build.json').write_text(json.dumps(dict(argv=argv, stdout=result.stdout, stderr=result.stderr), indent=2))
    if result.returncode:
        raise RuntimeError(result.stderr)
    os.sched_setaffinity(0, {args.cpu})
    methods = {'fsst': ['fsst', '1000'], 'lz4': ['lz4', '1000']}
    for name, folder in [('onpairplus', 'onpairplus'), ('astra', 'astra-rows')]:
        methods[name] = ['native', str(build / folder / 'encoder.so'), str(build / folder / 'decoder.so')]
    env = dict(os.environ, DEBUG='1', LC_ALL='C', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1')
    env.pop('LOOP', None)
    records = []
    for repeat in range(1 if args.smoke else 3):
        names = list(methods)
        for name in names[repeat:] + names[:repeat]:
            print('Replay', repeat + 1, name, flush=True)
            result = subprocess.run([str(out / 'filtertest'), *methods[name], *map(str, inputs)],
                                    env=env, capture_output=True, text=True, timeout=360)
            for stream in ('stdout', 'stderr'):
                (out / f'{repeat + 1}-{name}.{stream}').write_text(getattr(result, stream))
            if result.returncode:
                raise RuntimeError(result.stderr)
            scores = {}
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) == 3 and fields[0] in {'1', '3', '10', '30', '100'}:
                    scores[fields[0]] = float(fields[2])
            if len(scores) != 5:
                raise RuntimeError('Incomplete paper benchmark output')
            size = int(next(line.split(':')[1] for line in result.stdout.splitlines() if line.startswith('# total compress size:')))
            records.append(dict(replay=repeat + 1, method=name, archive_bytes=size, krows_per_second=scores))
            (out / 'trials.json').write_text(json.dumps(records, indent=2) + '\n')
    summary = {name: {p: statistics.median(r['krows_per_second'][p] for r in records if r['method'] == name)
                     for p in ('1', '3', '10', '30', '100')} for name in methods}
    (out / 'summary.json').write_text(json.dumps(dict(smoke=args.smoke, cpu=args.cpu, krows_per_second=summary), indent=2) + '\n')


if __name__ == '__main__':
    main()
