"""Optional WASM decoder build with the flags used in the recorded pilot."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from urllib.request import urlopen

HERE = Path(__file__).resolve().parent
PINS = {'lz4.c': 'b6a85fd8f9be0fedb568abd1338719b23b999583ccda6f3404d5ae11e4ce7b8e',
        'lz4.h': 'c1614ecf7ada7b0be1acb560d4239595f96fbb7aa6a79a7c40cb358753830be6'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=False)
    for name, digest in PINS.items():
        with urlopen('https://raw.githubusercontent.com/lz4/lz4/v1.9.4/lib/' + name, timeout=30) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != digest:
            raise RuntimeError('LZ4 source hash mismatch')
        (out / name).write_bytes(data)
    commands = []

    def run(argv):
        commands.append(list(map(str, argv)))
        (out / 'build.json').write_text(json.dumps(commands, indent=2) + '\n')
        subprocess.run(commands[-1], check=True, timeout=180)

    run(['emcc', '-O3', '-msimd128', '-c', out / 'lz4.c', '-o', out / 'lz4.o'])
    for mode in ('bulk', 'rows'):
        source = HERE.parent / 'dbtext/candidates' / ('astra-' + mode)
        run(['em++', source / 'decoder.cpp', '-std=c++20', '-O3', '-DNDEBUG', '-msimd128',
             '-mssse3', '-fno-exceptions', '-sFILESYSTEM=0', '-sALLOW_MEMORY_GROWTH=1',
             '-sINITIAL_MEMORY=67108864', '--no-entry',
             '-sEXPORTED_FUNCTIONS=["_lab_open","_lab_decode","_lab_rows","_lab_close","_malloc","_free"]',
             *(['-I' + str(out), out / 'lz4.o'] if mode == 'bulk' else []),
             '-o', out / (mode + '.wasm')])


if __name__ == '__main__':
    main()
