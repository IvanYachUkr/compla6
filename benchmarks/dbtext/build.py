"""Build the original baseline wrappers and selected native candidates."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

HERE = Path(__file__).resolve().parent
UPSTREAM = HERE.parent / 'upstream'


def build(output):
    output = Path(output).resolve()
    output.mkdir(parents=True, exist_ok=True)
    records = []

    def run(argv):
        argv = list(map(str, argv))
        result = subprocess.run(argv, capture_output=True, text=True, timeout=180)
        records.append(dict(argv=argv, returncode=result.returncode,
                            stdout=result.stdout, stderr=result.stderr))
        (output / 'build.json').write_text(json.dumps(records, indent=2) + '\n')
        if result.returncode:
            raise RuntimeError(result.stderr)

    if not (UPSTREAM / 'token/src').exists():
        raise RuntimeError('First run: python3 benchmarks/upstream/fetch.py')
    run(['cmake', '-S', UPSTREAM / 'fsst', '-B', output / 'fsst-lib',
         '-DBUILD_SHARED_LIBS=ON', '-DCMAKE_BUILD_TYPE=Release'])
    run(['cmake', '--build', output / 'fsst-lib', '--target', 'fsst', '--parallel', '2'])
    run(['g++', '-std=c++17', '-O3', '-DNDEBUG', HERE / 'driver.cpp', '-ldl', '-o', output / 'driver'])
    common = ['g++', '-std=c++23', '-O3', '-DNDEBUG', '-fPIC', '-shared',
              '-I' + str(HERE), '-I' + str(UPSTREAM / 'fsst'),
              '-L' + str(output / 'fsst-lib')]
    methods = []
    for name, number, library, variant in [('lz4', 0, 'lz4', 'bulk'),
                                          ('zstd1', 1, 'zstd', 'bulk'), ('fsst', 2, 'fsst', 'rows')]:
        folder = output / name
        folder.mkdir(exist_ok=True)
        for role in ('encoder', 'decoder'):
            run([*common, f'-DMETHOD={number}', *(['-DDECODE_ONLY'] if role == 'decoder' else []),
                 HERE / 'reference.cpp', '-l' + library, '-o', folder / (role + '.so')])
        methods.append((name, variant))
    token = UPSTREAM / 'token/src'
    folder = output / 'onpairplus'
    folder.mkdir(exist_ok=True)
    # Match the measured wrapper flags; the algorithm remains upstream OnPair+.
    run([*common, '-include', 'span', '-include', 'string', '-include', 'cstring',
         '-include', 'stdexcept', '-I' + str(token), '-fno-tree-vectorize', HERE / 'onpair.cpp',
         HERE / 'onpair-base.cpp', token / 'compressor/onpair_advanced/OnPairAdvancedCompressor.cpp',
         token / 'compressor/onpair_advanced/LongestPrefixMatcher.cpp', '-o', folder / 'encoder.so'])
    run([*common, '-fno-tree-vectorize', '-DDECODE_ONLY', HERE / 'onpair.cpp', '-o', folder / 'decoder.so'])
    methods.append(('onpairplus', 'rows'))
    for entry in json.loads((HERE / 'methods.json').read_text()):
        source = HERE / 'candidates' / entry['id']
        folder = output / entry['id']
        folder.mkdir(exist_ok=True)
        for rel, expected in entry['source_sha256'].items():
            if hashlib.sha256((source / rel).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Candidate source changed: ' + rel)
        vendor = folder / 'vendor'
        shutil.copytree(UPSTREAM / 'token', vendor, dirs_exist_ok=True)
        if (source / 'vendor-overrides').exists():
            shutil.copytree(source / 'vendor-overrides', vendor, dirs_exist_ok=True)
        for command in entry['build_commands']:
            # Relocate upstream dependencies without editing the measured sources.
            run([arg.replace('{source}/vendor', str(vendor)).replace('{source}', str(source))
                    .replace('{build}', str(folder)) for arg in command])
        methods.append((entry['id'], entry['variant']))
    for name, variant in methods:
        (output / name / 'manifest.json').write_text(json.dumps(dict(
            name=name, variant=variant, encoder='encoder.so', decoder='decoder.so')) + '\n')
    return output


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    print(build(parser.parse_args().out))
