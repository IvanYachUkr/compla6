"""Pinned, separately linked decoder controls with deterministic native workers."""
from __future__ import annotations
import os, shutil, struct, tempfile
from pathlib import Path
from .util import Error, load, save, sha
from .runner import execute
from .candidate import check_limits,execution_timeout,checked_sha,checked_copy

CONFIGS=[('stored',0,False),('zstd',1,False),('zstd',3,False),('zstd',9,False),
         ('zstd',1,True),('zstd',3,True),('lz4',1,False),('brotli',1,False),
         ('brotli',4,False),('xz',1,False),('xz',3,False),('zlib',1,False),
         ('zlib',6,False),('bzip2',1,False),('bzip2',9,False)]
LIBS={'stored':[], 'zstd':['libzstd.a'], 'lz4':['liblz4.a'],
      'brotli':['libbrotlienc.a','libbrotlidec.a','libbrotlicommon.a'],
      'xz':['liblzma.a'], 'zlib':['libz.a'], 'bzip2':['libbz2.a']}
HEADERS={'stored':[], 'zstd':['zstd.h','zstd_errors.h','zdict.h'],
         'lz4':['lz4.h','lz4hc.h','lz4frame.h','lz4file.h'],
         'brotli':['brotli'], 'xz':['lzma.h','lzma'], 'zlib':['zlib.h','zconf.h'],
         'bzip2':['bzlib.h']}

def name(family,level,dictionary=False):
 return 'parallel-'+family+('' if family=='stored' else '-'+str(level))+('-dict64k' if dictionary else '')

def recipes():
 return {name(f,l,d):{'family':f,'level':l,'use_dictionary':d,'native_parallel':True,
                     'kind':'conventional-independent-chunk-control'} for f,l,d in CONFIGS}

def prefix_info(prefix=None):
 value=prefix or os.environ.get('COMPRESSION_LAB_NATIVE_PREFIX')
 if not value:raise Error('pinned_native_prefix_unavailable','Configure the evaluator-owned COMPRESSION_LAB_NATIVE_PREFIX')
 root=Path(value).resolve();receipt=root/'native-build-receipt.json'
 if not receipt.is_file():receipt=root.parent/'receipts/native-build-receipt.json'
 if not receipt.is_file():raise Error('pinned_native_receipt_missing')
 info=load(receipt)
 if info.get('status')!='verified':raise Error('pinned_native_build_unverified')
 return root,info

TRAINER=r'''#include <zdict.h>
#include <cstdint>
#include <fstream>
#include <iterator>
#include <vector>
#include <stdexcept>
int main(int argc,char**argv){try{
 if(argc!=3)return 2;std::ifstream f(argv[1],std::ios::binary);
 std::vector<char>b((std::istreambuf_iterator<char>(f)),{});
 if(b.empty()||b.size()%4096)return 3;
 std::vector<size_t> sizes(b.size()/4096,4096);std::vector<char>d(65536);
 size_t n=ZDICT_trainFromBuffer(d.data(),d.size(),b.data(),sizes.data(),sizes.size());
 if(ZDICT_isError(n)||n==0)return 4;std::ofstream out(argv[2],std::ios::binary);
 out.write(d.data(),n);return out?0:5;
 }catch(...){return 6;}}
'''

def train_dictionary(rows,prefix,limits=None,*,fitting_partition='train'):
 """Fixed, spread samples from training only; native training is outside timing."""
 limits=limits or {};check_limits(limits)
 train=sorted((r for r in rows if r['split']==fitting_partition),key=lambda r:(r['canonical_sha256'],r['alias']))
 if not train:raise Error('insufficient_dictionary_training')
 samples=[];used=[];budget=8*1024*1024;chunk=4096
 for index,row in enumerate(train):
  check_limits(limits)
  p=Path(row['source'])
  if checked_sha(p,limits)!=row['canonical_sha256']:raise Error('stale_training_digest',row['alias'])
  length=p.stat().st_size;count=min(length//chunk,budget//chunk//(len(train)-index))
  if not count:continue
  positions=[i*(length-chunk)//max(1,count-1) for i in range(count)]
  with p.open('rb') as f:
   for pos in positions:
    check_limits(limits);f.seek(pos);samples.append(f.read(chunk));check_limits(limits)
  budget-=count*chunk;used.append(row['canonical_sha256'])
 if len(samples)<8:raise Error('insufficient_dictionary_training')
 with tempfile.TemporaryDirectory(prefix='compression-lab-dictionary-') as temp:
  root=Path(temp);source=root/'source';source.mkdir();output=root/'build';output.mkdir()
  (source/'trainer.cpp').write_text(TRAINER);(source/'samples.bin').write_bytes(b''.join(samples))
  for asset in ('include/zdict.h','include/zstd.h','include/zstd_errors.h','lib/libzstd.a'):
   checked_copy(prefix/asset,source/Path(asset).name,limits)
  execute(['g++','-O3','-I/source','/source/trainer.cpp','/source/libzstd.a','-pthread','-o','/output/trainer'],output=output,readonly={'/source':source},build=True,timeout=execution_timeout(limits),memory=limits.get('memory_bytes',2*1024**3),cancel=limits.get('_cancel'))
  check_limits(limits)
  # Only platform libraries are used by this trusted, statically linked trainer.
  from .candidate import closure,runtime_mounts
  dep=closure(output/'trainer',{'dependencies':[]},limits=limits);mounts=runtime_mounts(dep,limits=limits)
  result=root/'result';result.mkdir()
  execute(['/candidate/trainer','/input/samples.bin','/output/dictionary.bin'],output=result,readonly={'/candidate':output,'/input':source},runtime_files=mounts,timeout=execution_timeout(limits),memory=limits.get('memory_bytes',2*1024**3),cancel=limits.get('_cancel'))
  check_limits(limits);data=(result/'dictionary.bin').read_bytes();check_limits(limits)
 return data,{'kind':'whole-dataset' if fitting_partition=='corpus' else 'train-only','object_sha256':used,'sampling_version':'spread-fixed-4096-v1','actual_sample_bytes':sum(map(len,samples)),'sample_cap_bytes_total':8*1024*1024,'chunk_bytes':4096,'dictionary_target_bytes':65536,'algorithm':'ZDICT_trainFromBuffer; pinned static Zstd','training_code':'trainer.cpp'}

def add_offline_dictionary(path,manifest,sampling,include_flags,link_args,limits=None):
 """Make the existing fitted artifact reproducible inside the measured pipeline."""
 checked_copy(Path(__file__).parent/'data/native/dictionary_prepare.cpp',path/'offline.cpp',limits)
 manifest['source_paths'].append('offline.cpp')
 tail=['-std=c++17','-ffile-prefix-map=/source=source','-ffile-prefix-map=/output=build',
       *include_flags,'{source}/offline.cpp',*link_args,'-o','{build}/offline']
 manifest['build_commands'].append(['g++','-O3','-DNDEBUG',*tail])
 manifest['sanitizer_build_commands'].append(['g++','-O1','-g','-fsanitize=address,undefined',
                                             '-fno-omit-frame-pointer','-no-pie',*tail])
 manifest['build_output_paths'].append('offline')
 manifest['offline']={'executable':'offline','command':['{runtime}/offline','{input_dir}','{output_dir}',sampling],
                      'artifact_paths':list(manifest['artifact_paths']),'rebuild':False}

def create(path,family='zstd',level=1,rows=(),use_dictionary=False,*,native_prefix=None,max_threads=64,limits=None,fitting_partition='train',offline_replay=False):
 check_limits(limits)
 if family not in LIBS or type(level) is not int or type(max_threads) is not int or not 1<=max_threads<=64:raise Error('invalid_native_control')
 bounds={'stored':(0,0),'zstd':(-5,22),'lz4':(1,12),'brotli':(0,11),'xz':(0,9),'zlib':(0,9),'bzip2':(1,9)}
 if not bounds[family][0]<=level<=bounds[family][1] or (family=='lz4' and level==2) or (use_dictionary and family!='zstd'):raise Error('invalid_native_control')
 prefix,receipt=prefix_info(native_prefix);check_limits(limits);path=Path(path);path.mkdir(parents=True)
 native=Path(__file__).parent/'data/native';source=native/'parallel_baseline.cpp'
 if not source.is_file():raise Error('parallel_native_source_unavailable')
 sources=[];runtime=[];assets=[]
 def copy(asset,target):
  check_limits(limits);target_path=path/target;target_path.parent.mkdir(parents=True,exist_ok=True);checked_copy(asset,target_path,limits);sources.append(target)
  assets.append({'path':target,'bytes':target_path.stat().st_size,'sha256':checked_sha(target_path,limits)})
 copy(source,'codec.cpp')
 for helper in ('lab_crc.hpp','parallel_workers.hpp'):copy(native/helper,helper)
 expected=receipt.get('packages',{}).get(family,{}).get('outputs',{})
 if family!='stored' and not expected:raise Error('pinned_native_family_missing',family)
 def copy_pinned(relative,target):
  p=prefix/relative
  if not p.is_file() or expected.get(relative)!=checked_sha(p,limits):raise Error('pinned_native_asset_changed',relative)
  copy(p,target)
 for lib in LIBS[family]:copy_pinned('lib/'+lib,'vendor/lib/'+lib)
 for h in HEADERS[family]:
  p=prefix/'include'/h
  for header in sorted(p.rglob('*')) if p.is_dir() else [p]:
   if header.is_file():
    relative=header.relative_to(prefix).as_posix();copy_pinned(relative,'vendor/'+relative)
 if family!='stored':
  for p in sorted((prefix/'share/licenses'/family).glob('*')):
   if p.is_file() and p.stat().st_size:
    relative=p.relative_to(prefix).as_posix();target='licenses/'+family+'/'+p.name;copy_pinned(relative,target);sources.remove(target);runtime.append(target)
  if not runtime:raise Error('dependency_license_missing',family)
 compiler=Path(shutil.which('g++',path='/usr/bin:/bin') or '/missing').resolve()
 if not compiler.is_file():raise Error('blocked_toolchain','g++')
 common=['-std=c++17','-pthread','-DCL_CODEC='+str(list(LIBS).index(family)),'-DCL_LEVEL='+str(level),'-DCL_THREADS='+str(max_threads),'-DCL_CHUNK_SIZE=4194304','-I{source}/vendor/include','-frandom-seed=compression-lab-v2','-ffile-prefix-map=/source=source','-ffile-prefix-map=/output=build','-ffunction-sections','-fdata-sections']
 builds=[];diagnostics=[]
 for decoder in (False,True):
  libs=[x for x in LIBS[family] if not (decoder and x=='libbrotlienc.a')]
  tail=[*common,*(['-DCL_DECODE_ONLY=1'] if decoder else []),'{source}/codec.cpp',*['{source}/vendor/lib/'+x for x in libs],'-lm','-Wl,--gc-sections','-o','{build}/'+('decoder' if decoder else 'codec')]
  builds.append(['g++','-O3','-DNDEBUG','-s',*tail]);diagnostics.append(['g++','-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer','-no-pie',*tail])
 commands={op:['{runtime}/'+('decoder' if op.startswith('decode') else 'codec'),op.replace('_','-')]+(['{input_dir}','{output_dir}'] if op.endswith('_dir') else []) for op in ('encode_dir','decode_dir','encode_stream','decode_stream')}
 artifacts=[];training={'kind':'data-independent'}
 if use_dictionary:
  data,training=train_dictionary(rows,prefix,limits=limits,fitting_partition=fitting_partition);check_limits(limits);(path/'dictionary.bin').write_bytes(data);artifacts=['dictionary.bin'];(path/'trainer.cpp').write_text(TRAINER);sources.append('trainer.cpp')
  for cmd in commands.values():cmd.append('{runtime}/dictionary.bin')
 pkg=receipt.get('packages',{}).get(family,{})
 build_info={'schema_version':1,'family':family,'version':pkg.get('version','stored-v2'),'source':{k:pkg[k] for k in ('archive','immutable_git_commit','release_date','release_url') if k in pkg},'native_version_probe':pkg.get('probe'),'compiler':receipt['compiler'],'assets':assets,'linked_codec':'static; actual decoder ELF closure verified independently'}
 save(path/'build-assets.json',build_info);sources.append('build-assets.json')
 manifest={'schema_version':2,'candidate_id':name(family,level,use_dictionary),'language':'c++','input_domain':'opaque_bytes','deterministic':True,'threads':max_threads,'source_paths':sources,'artifact_paths':artifacts,'runtime_paths':runtime,'build_commands':builds,'sanitizer_build_commands':diagnostics,'build_output_paths':['codec','decoder'],'executable':'codec','commands':commands,'toolchain':{'g++':{'path':str(compiler),'sha256':checked_sha(compiler,limits)}},'dependencies':[],'training':training,'decoder':{'executable':'decoder','build_output_paths':['decoder'],'artifact_paths':artifacts,'runtime_paths':runtime},'diagnostics':{'kind':'cxx-asan-ubsan-v1'},'hypothesis':{'kind':'conventional-independent-chunk-control','family':family,'version':pkg.get('version','stored-v2'),'level':level,'chunk_bytes':4194304,'worker_policy':'main included; runtime worker count capped by evaluator profile','link_mode':'pinned-static-codec','statement':'Independent deterministic chunks use established native codec APIs; no model or Python binding in timed execution.'}}
 if use_dictionary and offline_replay:
  add_offline_dictionary(path,manifest,'spread',['-I{source}/vendor/include'],['{source}/vendor/lib/libzstd.a'],limits)
 check_limits(limits);save(path/'candidate.json',manifest);return path
