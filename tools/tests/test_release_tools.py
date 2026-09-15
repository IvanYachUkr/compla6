import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def module(name):
    spec=importlib.util.spec_from_file_location(name,ROOT/'tools'/f'{name}.py')
    result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result

class SourceFetchTests(unittest.TestCase):
    def test_correct_hash_and_mismatch(self):
        fetch=module('fetch_upstream')
        with tempfile.TemporaryDirectory() as td:
            p=Path(td)/'file';p.write_bytes(b'known source')
            self.assertEqual(fetch.verify(p,hashlib.sha256(b'known source').hexdigest()),fetch.digest(p))
            with self.assertRaises(ValueError):fetch.verify(p,'0'*64)
    def test_regular_archive_and_mode(self):
        fetch=module('fetch_upstream')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);archive=root/'a.tar.gz'
            with tarfile.open(archive,'w:gz') as f:
                item=tarfile.TarInfo('source/file');item.size=3;item.mode=0o4777;f.addfile(item,io.BytesIO(b'abc'))
            fetch.extract_regular_tar(archive,root/'out')
            self.assertEqual((root/'out/source/file').read_bytes(),b'abc')
            self.assertEqual((root/'out/source/file').stat().st_mode&0o7777,0o755)
    def test_archive_path_link_duplicate_rejected(self):
        fetch=module('fetch_upstream')
        for kind in ('parent','absolute','backslash','symlink','duplicate'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as td:
                root=Path(td);archive=root/'a.tar'
                with tarfile.open(archive,'w') as f:
                    path={'parent':'../escape','absolute':'/escape','backslash':'a\\escape'}.get(kind,'source/file')
                    item=tarfile.TarInfo(path)
                    if kind=='symlink':item.type=tarfile.SYMTYPE;item.linkname='../outside'
                    else:item.size=1
                    f.addfile(item,io.BytesIO(b'x'))
                    if kind=='duplicate':f.addfile(item,io.BytesIO(b'x'))
                with self.assertRaises(ValueError):fetch.extract_regular_tar(archive,root/'out')
                self.assertFalse((root/'escape').exists())

class ProbeTests(unittest.TestCase):
    def test_only_explicit_loopback_endpoint(self):
        probe=module('mcp_probe');self.assertEqual(probe.loopback_url('http://127.0.0.1:8766/mcp'),'http://127.0.0.1:8766/mcp')
        for value in ('https://example.org/mcp','http://localhost:8766/mcp','http://127.0.0.1/mcp','http://a:b@127.0.0.1:80/mcp','http://127.0.0.1:8766/mcp?token=x','http://127.0.0.1:8766/mcp#x','file:///mcp','http://127.0.0.1:99999/mcp'):
            with self.subTest(value=value),self.assertRaises(ValueError):probe.loopback_url(value)

class ReproductionTests(unittest.TestCase):
    def test_generator_is_exact_reproducible_disjoint(self):
        bench=module('benchmark_redesign')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);bench.generate(root/'a',65536);bench.generate(root/'b',65536)
            a=json.loads((root/'a/manifest.json').read_text());b=json.loads((root/'b/manifest.json').read_text())
            self.assertEqual(a,b);rows=a['objects'];self.assertEqual(len(rows),6)
            self.assertEqual(len({r['canonical_sha256'] for r in rows}),6)
            self.assertEqual(sum(r['canonical_bytes'] for r in rows),6*65536)
    def test_observation_is_offline_and_distinguishes_pin(self):
        observer=module('observe_runtime')
        report=observer.observe()
        self.assertEqual(report['schema_version'],1)
        self.assertFalse(report['network_requests'])
        self.assertFalse(report['claims_latest_installed'])
        self.assertIn('runtime_fingerprint',report)
        self.assertIn('python',report)
        self.assertEqual(len(report['python']['executable_sha256']),64)
        self.assertIn('zstd',report['native']['families'])
    def test_prime_launcher_has_no_implicit_version_and_preserves_mount_order(self):
        launcher=ROOT/'tools/prime_launch_linux.sh'
        subprocess.run(['bash','-n',str(launcher)],check=True)
        text=launcher.read_text()
        self.assertNotIn('/opt/prime-agent-0.9.2',text)
        self.assertIn('COMPRESSION_LAB_PRIME_EXECUTABLE',text)
        self.assertLess(text.index('--bind "$LAB_PROFILE" "$LAB_AGENT_HOME"'),text.index('--ro-bind "$LAB_PRIME_ROOT"'))


class ReleasePackagingTests(unittest.TestCase):
    def test_selection_includes_new_contracts_not_caches(self):
        pack=module('package_release');pack.RELEASE_RESULTS=set()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('docs/ARCHITECTURE_0_3.md','dependencies/upstream-lock.json','src/compression_lab/core.py','tools/tests/test_any.py','src/compression_lab/data/native/parallel_baseline.cpp','src/compression_lab/__pycache__/core.pyc','results/redesign/job.pid','docs/build/stale.bin','results/downloads/archive.tar','dependencies/evidence/private.json','src/.venv/pyvenv.cfg'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x')
            names={p.relative_to(root).as_posix() for p in pack.selected_files(root)}
            self.assertEqual(names,{'docs/ARCHITECTURE_0_3.md','dependencies/upstream-lock.json','src/compression_lab/core.py','tools/tests/test_any.py','src/compression_lab/data/native/parallel_baseline.cpp'})
    def test_selection_keeps_explicit_receipt_without_archived_workspaces(self):
        pack=module('package_release')
        pack.RELEASE_RESULTS={'results/fresh/summary.json'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td)
            for name in ('results/fresh/summary.json','results/fresh/work/public/object.bin',
                         'results/old/summary.json','dependencies/observed-runtime.json',
                         'dependencies/observed-runtime-release.json','dependencies/native-lock.json',
                         'provenance/compression-lab-review-input.zip'):
                p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'x')
            names={p.relative_to(root).as_posix() for p in pack.selected_files(root)}
            self.assertEqual(names,{'results/fresh/summary.json','dependencies/native-lock.json'})
    def test_selection_requires_named_receipt_to_exist(self):
        pack=module('package_release');pack.RELEASE_RESULTS={'results/fresh/summary.json'}
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError,'Missing release validation receipt'):
                pack.selected_files(Path(td))
    def test_selection_rejects_link(self):
        pack=module('package_release');pack.RELEASE_RESULTS=set()
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'src').mkdir();(root/'src/link').symlink_to('/etc/passwd')
            with self.assertRaises(ValueError):pack.selected_files(root)
    def test_selection_rejects_linked_receipt_parent(self):
        pack=module('package_release');pack.RELEASE_RESULTS={'results/summary.json'}
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source';source.mkdir();outside=root/'outside';outside.mkdir()
            (outside/'summary.json').write_text('{}');(source/'results').symlink_to(outside,target_is_directory=True)
            with self.assertRaisesRegex(ValueError,'symlink'):
                pack.selected_files(source)
    def test_manifest_and_zip_roundtrip(self):
        pack=module('package_release')
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);stage=root/'example';stage.mkdir();p=stage/'entry';p.write_bytes(b'\x00\xff\r\n');p.chmod(0o755)
            manifest=pack.manifest(stage,'test');(stage/'RELEASE_MANIFEST.json').write_text(json.dumps(manifest))
            archive=root/'out.zip';pack.make_zip(stage,archive)
            report=pack.verify_zip(archive,stage.name,manifest)
            self.assertEqual(report['verified_files'],1);self.assertTrue(report['file_modes_preserved'])

    def test_selected_release_preserves_usable_replay_inputs(self):
        pack=module('package_release');pack.RELEASE_RESULTS=set()
        names={p.relative_to(ROOT).as_posix() for p in pack.selected_files(ROOT)}
        references={'provenance/research-completion-parent.json','provenance/completion-controls-parent.json','provenance/controller-before-policy.py','provenance/original-native/baseline.cpp',
                    'provenance/base-controller-cleanup-051-manifest.json','provenance/prime-integration-inputs.json'}
        self.assertEqual({name for name in names if name.startswith('provenance/')},references)
        with tempfile.TemporaryDirectory() as td:
            stage=Path(td)/'application';stage.mkdir()
            for name in references:pack.copy_file(ROOT/name,stage/name)
            record=pack.manifest(stage,'test');(stage/'RELEASE_MANIFEST.json').write_text(json.dumps(record))
            archive=Path(td)/'application.zip';pack.make_zip(stage,archive)
            unpacked=Path(td)/'unpacked'
            with zipfile.ZipFile(archive) as z:z.extractall(unpacked)
            for name in references:self.assertEqual((unpacked/'application'/name).read_bytes(),(ROOT/name).read_bytes())
            impact=module('controller_impact')
            before,provenance=impact.load_before(unpacked/'application/provenance/controller-before-policy.py')
            self.assertIn('public_decision',provenance)
            state={'budgets':{'agent':{'evaluations':1}},'search_budget':{'candidate_evaluations':1}}
            self.assertEqual(before['public_decision'](state,[])['action'],'budget_exhausted')

    def test_output_only_builds_one_application_root_with_optional_mcp_wheels(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);source=root/'source';(source/'tools').mkdir(parents=True)
            for name in ('package_release.py','verify_release.py'):
                shutil.copy2(ROOT/'tools'/name,source/'tools'/name)
            (source/'pyproject.toml').write_text('[project]\nversion = "0.0.0"\n')
            for name in module('package_release').RELEASE_RESULTS:
                p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('{}')
            for name in ('README.md','dist/compression_lab-0.0.0-py3-none-any.whl',
                         'dist/compression_lab-older-py3-none-any.whl',
                         'provenance/controller-before-policy.py','provenance/original-native/baseline.cpp',
                         'wheelhouse/mcp-linux-x86_64-cp312/mcp.whl',
                         'wheelhouse/build-tools/setuptools.whl'):
                p=source/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(b'fixture')
            for suffix,options,with_mcp in (('default',[],True),('core',['--without-mcp-wheels'],False)):
                with self.subTest(distribution=suffix):
                    out=root/suffix
                    command=[sys.executable,str(source/'tools/package_release.py'),'--output',str(out),*options]
                    result=subprocess.run(command,capture_output=True,text=True)
                    self.assertEqual(result.returncode,0,result.stdout+result.stderr)
                    receipt=json.loads((out/'release-receipt.json').read_text())
                    archive=out/receipt['archive'];prefix='compression-lab-0.0.0/'
                    self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),receipt['sha256'])
                    with zipfile.ZipFile(archive) as z:
                        names=set(z.namelist())
                        self.assertTrue(all(name.startswith(prefix) for name in names))
                        self.assertIn(prefix+'README.md',names)
                        self.assertIn(prefix+'provenance/controller-before-policy.py',names)
                        self.assertIn(prefix+'provenance/original-native/baseline.cpp',names)
                        self.assertIn(prefix+'dist/compression_lab-0.0.0-py3-none-any.whl',names)
                        self.assertNotIn(prefix+'dist/compression_lab-older-py3-none-any.whl',names)
                        self.assertIn(prefix+'wheelhouse/build-tools/setuptools.whl',names)
                        self.assertEqual(prefix+'wheelhouse/mcp-linux-x86_64-cp312/mcp.whl' in names,with_mcp)
                        self.assertEqual(sum(name.endswith('/RELEASE_MANIFEST.json') for name in names),1)
                        self.assertFalse(any('/host-adapters/' in name or '/compression-lab/' in name for name in names))
                        previous=json.loads(z.read(prefix+'provenance/PREVIOUS_RELEASE.json'))
                        self.assertEqual(previous['version'],'0.6.3')
                        self.assertEqual(len(previous['installed_manifest_sha256']),64)
                    rerun=subprocess.run(command,capture_output=True,text=True)
                    self.assertNotEqual(rerun.returncode,0)
                    self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),receipt['sha256'])
            (source/'dist').rename(source/'linked-dist')
            (source/'dist').symlink_to(source/'linked-dist',target_is_directory=True)
            linked=subprocess.run([sys.executable,str(source/'tools/package_release.py'),
                                   '--output',str(root/'linked')],capture_output=True,text=True)
            self.assertNotEqual(linked.returncode,0)
            self.assertFalse((root/'linked').exists())

    def test_verifier_rejects_lost_executable_mode(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);script=root/'run.sh';script.write_bytes(b'#!/bin/sh\nexit 0\n');script.chmod(0o755)
            record={'schema_version':1,'version':'test','files':[{'path':'run.sh','bytes':17,
                    'sha256':hashlib.sha256(b'#!/bin/sh\nexit 0\n').hexdigest(),'mode':0o755}]}
            (root/'RELEASE_MANIFEST.json').write_text(json.dumps(record))
            command=[sys.executable,str(ROOT/'tools/verify_release.py'),str(root)]
            good=subprocess.run(command,capture_output=True,text=True)
            self.assertEqual(good.returncode,0,good.stdout+good.stderr)
            script.chmod(0o644)
            bad=subprocess.run(command,capture_output=True,text=True)
            self.assertNotEqual(bad.returncode,0)
            self.assertIn('mode',bad.stdout)

    def test_verifier_rejects_forbidden_tree(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'README.md').write_bytes(b'ok');(root/'evidence').mkdir();(root/'evidence'/'secret').write_bytes(b'no')
            manifest={'schema_version':1,'version':'test','files':[{'path':'README.md','bytes':2,'sha256':hashlib.sha256(b'ok').hexdigest(),'mode':0o644}]}
            (root/'RELEASE_MANIFEST.json').write_text(json.dumps(manifest))
            result=subprocess.run([sys.executable,str(ROOT/'tools'/'verify_release.py'),str(root)],capture_output=True,text=True)
            self.assertNotEqual(result.returncode,0)
            self.assertIn('forbidden_tree',result.stdout)

if __name__=='__main__':unittest.main()
