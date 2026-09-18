"""Build and replay the Python/Yelp RAM benchmark on caller-supplied data."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import resource
import statistics
import subprocess
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / 'src'))
from compression_lab.benchmark_run import measurement_lock, write_metadata


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', choices=['python', 'yelp'], required=True)
    parser.add_argument('--input', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--cpu', type=int, default=min(os.sched_getaffinity(0)))
    parser.add_argument('--only', nargs='+', help='Method IDs, or lz4-1/zstd-3/zstd-19')
    parser.add_argument('--build-only', action='store_true')
    parser.add_argument('--smoke', action='store_true', help='Allow a small input; do not compare it with published data')
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    methods = [m for m in json.loads((HERE / 'methods.json').read_text()) if m['dataset'] == args.dataset]
    baselines = ['lz4-1', 'zstd-3', 'zstd-19']
    if args.only:
        known = set(baselines) | {m['id'] for m in methods}
        if set(args.only) - known:
            parser.error('Unknown method: ' + ', '.join(set(args.only) - known))
        methods = [m for m in methods if m['id'] in args.only]
        baselines = [b for b in baselines if b in args.only]
    builds = []

    def build(argv):
        result = subprocess.run(list(map(str, argv)), capture_output=True, text=True, timeout=180)
        builds.append(dict(argv=list(map(str, argv)), returncode=result.returncode,
                           stdout=result.stdout, stderr=result.stderr))
        (out / 'build.json').write_text(json.dumps(builds, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(result.stderr)

    build(['g++', '-O3', '-std=c++17', HERE / 'ram_bench.cpp', '-ldl', '-o', out / 'ram_bench'])
    if baselines:
        for phase in ('encode', 'decode'):
            build(['g++', '-O3', '-DNDEBUG', '-std=c++17', '-fPIC', '-shared', '-Wl,-Bsymbolic',
                   HERE / 'baseline.cpp', '-lzstd', '-llz4', '-o', out / ('baseline-' + phase + '.so'),
                   *(['-DRAM_ENCODE=1'] if phase == 'encode' else [])])
    for method in methods:
        folder = HERE / 'candidates' / method['id']
        for rel, expected in method['source_sha256'].items():
            if sha(folder / rel) != expected:
                raise RuntimeError('Candidate source changed: ' + method['id'] + '/' + rel)
        for index, phase in enumerate(('encode', 'decode')):
            command = []
            for part in method['build_commands'][index]:
                if part.endswith('.cpp'):
                    part = str(folder / (phase + '.cpp'))
                if part.endswith('/vendor/lib/libzstd.a'):
                    part = subprocess.check_output(['g++', '-print-file-name=libzstd.a'], text=True).strip()
                    if not Path(part).is_file():
                        raise RuntimeError('Install libzstd-dev (static archive required).')
                part = part.replace('{source}', str(folder / 'source')).replace('{build}', str(out))
                command.append(part)
            command[command.index('-o') + 1] = str(out / (method['id'] + '-' + phase + '.so'))
            build([*command, '-fPIC', '-shared', '-Wl,-Bsymbolic'])
    if args.build_only:
        return
    if args.input is None:
        parser.error('--input is required unless --build-only is used')
    source = args.input.resolve()
    pin = json.loads((HERE / 'inputs.json').read_text())[args.dataset]
    if not args.smoke and (source.stat().st_size != pin['bytes'] or sha(source) != pin['sha256']):
        raise RuntimeError('Input does not match the published dataset hash')
    os.sched_setaffinity(0, {args.cpu})

    def limits():
        resource.setrlimit(resource.RLIMIT_AS, (4 * 1024**3,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (900, 905))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    trials = 1 if args.smoke else 3
    metadata = write_metadata(out, protocol='whole-ram-v1', dataset=args.dataset,
        inputs={args.dataset: source}, cpu=args.cpu, smoke=args.smoke,
        parameters=dict(trials=trials, warmups=0, aggregation='median whole-corpus time',
                        decode='fresh codec call', memory_bytes=4 * 1024**3),
        artifacts=[out / 'ram_bench', *out.glob('*.so')])
    rows = []
    with measurement_lock():
        for entry in [dict(id=b, baseline=True) for b in baselines] + methods:
            name = entry['id']
            prefix = 'baseline' if entry.get('baseline') else name
            archive = out / (name + '.archive')
            env = dict(os.environ, COMPRESSION_LAB_THREADS='1', OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1',
                       RAM_METHOD=name, RAM_ORIGINAL_BYTES=str(source.stat().st_size))
            argv = [out / 'ram_bench', source, out / (prefix + '-encode.so'), out / (prefix + '-decode.so'),
                    '-', archive, str(trials), '0' if entry.get('baseline') else '34']
            print('Measuring', name, flush=True)
            result = subprocess.run(list(map(str, argv)), env=env, capture_output=True, text=True,
                                    preexec_fn=limits, timeout=960)
            (out / (name + '.jsonl')).write_text(result.stdout)
            (out / (name + '.stderr')).write_text(result.stderr)
            if result.returncode:
                raise RuntimeError(result.stderr)
            records = [json.loads(line) for line in result.stdout.splitlines()]
            if len(records) != trials or not all(r['exact'] for r in records):
                raise RuntimeError('Incomplete or inexact benchmark')
            row = dict(id=name, trials=records, payload_sha256=sha(archive), archive_bytes=records[0]['archive_bytes'])
            for phase in ('encode', 'decode'):
                row[phase + '_MB_s'] = source.stat().st_size / statistics.median(r[phase + '_seconds'] for r in records) / 1e6
            if not args.smoke and not entry.get('baseline') and row['archive_bytes'] != entry['prior_archive_bytes']:
                raise RuntimeError('Rebuilt archive size differs from the recorded result; inspect library versions.')
            if not args.smoke and not entry.get('baseline') and row['payload_sha256'] != entry['payload_sha256']:
                raise RuntimeError('Rebuilt payload differs from the frozen server archive; inspect library versions.')
            rows.append(row)
            (out / 'summary.json').write_text(json.dumps(dict(dataset=args.dataset, cpu=args.cpu, smoke=args.smoke,
                 input_sha256=metadata['inputs'][0]['sha256'], original_bytes=metadata['original_bytes'],
                 rows=rows), indent=2) + '\n')


if __name__ == '__main__':
    main()
