"""Observed build capabilities, not a claim that system packages are the latest."""
from __future__ import annotations
from pathlib import Path
from . import baselines, candidate
from .util import load, sha, Error


def inspect():
    known = candidate.libraries()
    lock = Path(__file__).parent/'data'/'upstream-stable.json'
    upstream = load(lock) if lock.exists() else {'verified_on': None, 'packages': {}}
    families = {}
    headers = {'zstd': ['zstd.h'], 'lz4': ['lz4.h'], 'brotli': ['brotli/encode.h', 'brotli/decode.h'], 'xz': ['lzma.h'], 'stored': []}
    for family, (_, _, names) in baselines.BACKENDS.items():
        missing = [n for n in names if n not in known]
        missing += [h for h in headers[family] if not (Path('/usr/include')/h).is_file()]
        version = None
        error = None
        if not missing:
            try:
                version = baselines.version(family, [known[n] for n in names])
            except (AttributeError, OSError, ValueError) as exc:
                error = str(exc)[:300]
        primary = known.get(names[0]) if names else None
        static = primary.parent/('lib'+family+'.a') if primary and family in ('zstd', 'lz4') else None
        current = upstream.get('packages', {}).get(family)
        families[family] = {
            'available': not missing and error is None, 'installed_version': version,
            'missing': missing, 'error': error,
            'libraries': [{'soname': n, 'path': str(known[n]), 'sha256': sha(known[n]),
                           'bytes': known[n].stat().st_size} for n in names if n in known],
            'static_codec_available': bool(static and static.is_file()),
            'static_codec_asset': {'path': str(static), 'sha256': sha(static), 'bytes': static.stat().st_size}
                                    if static and static.is_file() else None,
            'upstream_stable': current,
            'installed_matches_verified_stable': version == current.get('version') if current else None}
    native_error=None
    try:
        from .native_baselines import prefix_info, LIBS
        prefix,receipt=prefix_info()
        for family,libs in LIBS.items():
            package=receipt['packages'].get(family,{})
            assets=[{'path':str(prefix/'lib'/lib),'sha256':sha(prefix/'lib'/lib),
                     'bytes':(prefix/'lib'/lib).stat().st_size} for lib in libs]
            valid=all(package.get('outputs',{}).get('lib/'+Path(a['path']).name)==a['sha256'] for a in assets)
            if not valid:raise Error('pinned_native_asset_changed',family)
            families[family]={**families.get(family,{}),'available':True,
                'system_installed_version':families.get(family,{}).get('installed_version'),
                'installed_version':package.get('version','stored-v2'),'static_codec_available':True,
                'installed_matches_verified_stable':package.get('version')==upstream.get('packages',{}).get(family,{}).get('version') if family!='stored' else None,
                'pinned_static_assets':assets,'native_version_probe':package.get('probe',{}).get('stdout'),
                'upstream_stable':{k:package.get(k) for k in ('version','release_date','release_url','source_url')},
                'native_prefix':str(prefix),'selected_mode':'pinned-static-native',
                'decoder_accounting':'Linked decoder bytes and actual ELF closure; build assets disclosed separately.'}
    except (Error,OSError,KeyError) as exc:
        native_error=str(exc)
        for family in ('zlib','bzip2'):
            families.setdefault(family,{'available':False,'installed_version':None,'static_codec_available':False,'error':native_error})
    rust=None
    try:
        identity=candidate.rust_identity()
        rust={k:identity[k] for k in ('path','sha256','sysroot_digest')}
        rust.update(diagnostics='rust-checked-v1',ASan=False,UBSan=False)
    except Error:pass
    return {'schema_version': 2, 'families': families, 'recipes': baselines.recipes(),
            'native_prefix_error':native_error,'rust_toolchain':rust,
            'upstream_verified_on': upstream.get('verified_on'),
            'scope': 'Read-only host library/header inventory; availability is not a successful build or quality result.',
            'deployed_library_cost_policy': 'Actual hashed ELF closure; static build assets also charged in source view. No runtime LLM.'}
