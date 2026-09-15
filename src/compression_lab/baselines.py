"""Conventional native controls; no Python binding participates in timing."""
from __future__ import annotations
import ctypes,shutil,sys,tempfile
from pathlib import Path
from .util import Error,save,sha
from .candidate import libraries,elf,PLATFORM,check_limits,execution_timeout,checked_sha,checked_copy
from .runner import execute
MATRIX=[('stored',0,False),('zstd',1,False),('zstd',3,False),('zstd',9,False),('zstd',1,True),('zstd',3,True),('lz4',0,False),('brotli',4,False),('xz',3,False)]
BACKENDS={'stored':(0,[],[]),'zstd':(1,['-lzstd'],['libzstd.so.1']),'lz4':(2,['-llz4'],['liblz4.so.1']),'brotli':(3,['-lbrotlienc','-lbrotlidec','-lbrotlicommon'],['libbrotlienc.so.1','libbrotlidec.so.1','libbrotlicommon.so.1']),'xz':(4,['-llzma'],['liblzma.so.5'])}
def version(name,paths):
 if not paths:return 'CLB1-v1'
 lib=ctypes.CDLL(str(paths[0]));fun={'zstd':'ZSTD_versionString','lz4':'LZ4_versionString','xz':'lzma_version_string'}.get(name)
 if fun:f=getattr(lib,fun);f.restype=ctypes.c_char_p;return f().decode()
 f=lib.BrotliEncoderVersion;f.restype=ctypes.c_uint32;n=f();return f'{n>>24}.{(n>>12)&4095}.{n&4095}'
def training_samples(rows,max_sample_bytes=8*1024*1024,limits=None,*,fitting_partition='train'):
 """Bounded prefix sampling for dictionary training (not screen object selection)."""
 check_limits(limits)
 if type(max_sample_bytes) is not int or not 32768<=max_sample_bytes<=8*1024*1024:raise Error('invalid_dictionary_sample_cap')
 chunks=[];used=[];total=0
 for row in sorted((r for r in rows if r['split']==fitting_partition),key=lambda r:(r['canonical_sha256'],r['alias'])):
  check_limits(limits)
  remaining=max_sample_bytes-total
  if remaining<=0:break
  # Hash each actually used object before sampling, not every public object.
  # Sample memory is bounded; provenance hashing may read larger source objects.
  if checked_sha(Path(row['source']),limits)!=row['canonical_sha256']:raise Error('stale_training_digest',row['alias'])
  with Path(row['source']).open('rb') as source:sample=source.read(min(128*1024,remaining))
  check_limits(limits)
  if not sample:continue
  chunks.extend(sample[i:i+4096] for i in range(0,len(sample),4096));used.append(row['canonical_sha256']);total+=len(sample)
 return chunks,{'kind':'whole-dataset' if fitting_partition=='corpus' else 'train-only','object_sha256':used,'sample_cap_bytes_per_object':128*1024,
   'sample_cap_bytes_total':max_sample_bytes,'actual_sample_bytes':total,'chunk_bytes':4096,
   'source_integrity':'Whole sampled source objects hash-verified before prefix read; hashing I/O is not limited to prefix bytes.',
   'algorithm':'ZDICT_trainFromBuffer','sampling_version':'bounded-hash-order-prefix-v2'}

_DICTIONARY_TRAINER=r'''import ctypes,json,pathlib,sys
lib=ctypes.CDLL(sys.argv[1]);samples=pathlib.Path(sys.argv[2]).read_bytes();lengths=json.loads(pathlib.Path(sys.argv[3]).read_text());size=int(sys.argv[4])
f=lib.ZDICT_trainFromBuffer;f.argtypes=[ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.POINTER(ctypes.c_size_t),ctypes.c_uint];f.restype=ctypes.c_size_t
lib.ZDICT_isError.argtypes=[ctypes.c_size_t];lib.ZDICT_isError.restype=ctypes.c_uint
buf=ctypes.create_string_buffer(samples);out=ctypes.create_string_buffer(size);sizes=(ctypes.c_size_t*len(lengths))(*lengths);n=f(out,size,buf,sizes,len(lengths))
if lib.ZDICT_isError(n):sys.exit(2)
pathlib.Path(sys.argv[5]).write_bytes(out.raw[:n])
'''

def dictionary(rows,size=8192,limits=None,*,fitting_partition='train'):
 limits=limits or {};check_limits(limits);known=libraries(limits);check_limits(limits)
 if 'libzstd.so.1' not in known:raise Error('baseline_unavailable','zstd')
 chunks,provenance=training_samples(rows,limits=limits,fitting_partition=fitting_partition)
 if len(chunks)<8:raise Error('insufficient_dictionary_training')
 with tempfile.TemporaryDirectory(prefix='compression-lab-dictionary-') as temp:
  root=Path(temp);source=root/'source';source.mkdir();output=root/'output';output.mkdir()
  (source/'samples.bin').write_bytes(b''.join(chunks));save(source/'sizes.json',list(map(len,chunks)));check_limits(limits)
  # Fixed evaluator code only; the existing runner owns the child process group.
  # Sampling and the ZDICT call are unchanged, while training is interruptible.
  try:
   execute([sys.executable,'-I','-S','-c',_DICTIONARY_TRAINER,str(known['libzstd.so.1']),'/input/samples.bin','/input/sizes.json',str(size),'/output/dictionary.bin'],
    output=output,readonly={'/input':source},mode='exploratory',timeout=execution_timeout(limits),memory=limits.get('memory_bytes',2*1024**3),output_limit=max(65536,size),cancel=limits.get('_cancel'))
  except Error as error:
   if error.code=='native_failed':raise Error('dictionary_training_failed','No development fallback') from error
   raise
  check_limits(limits);data=(output/'dictionary.bin').read_bytes();check_limits(limits)
 return data,{**provenance,'dictionary_target_bytes':size}

def create(path,family='zstd',level=1,rows=(),use_dictionary=False,*,transform='identity',link_mode='shared',native_parallel=False,native_prefix=None,max_threads=64,limits=None,fitting_partition='train',offline_replay=False):
 check_limits(limits)
 if native_parallel:
  from .native_baselines import create as create_native
  return create_native(path,family,level,rows,use_dictionary,native_prefix=native_prefix,max_threads=max_threads,limits=limits,fitting_partition=fitting_partition,offline_replay=offline_replay)
 if family not in BACKENDS:raise Error('unknown_baseline')
 if transform not in ('identity','delta8','shuffle4'):raise Error('unknown_transform')
 if link_mode not in ('shared','static-codec'):raise Error('unknown_link_mode')
 if link_mode=='static-codec' and family not in ('zstd','lz4'):raise Error('static_codec_unsupported')
 if family=='stored' and level==1:level=0
 if family=='lz4' and level==1:level=0
 if type(level) is not int or not {'stored':(0,0),'zstd':(-5,22),'lz4':(0,0),'brotli':(0,11),'xz':(0,9)}[family][0]<=level<={'stored':(0,0),'zstd':(-5,22),'lz4':(0,0),'brotli':(0,11),'xz':(0,9)}[family][1]:raise Error('invalid_codec_level')
 if use_dictionary and transform!='identity':raise Error('dictionary_transform_requires_matched_training','Train transformed samples explicitly; raw dictionary factory is identity-only')
 path=Path(path);path.mkdir(parents=True);code,link,names=BACKENDS[family];known=libraries(limits);check_limits(limits)
 if any(n not in known for n in names):raise Error('baseline_unavailable',family)
 ver=version(family,[known[n] for n in names]);primary=set(names);names=list(names);seen=set(names)
 for n in names:
  check_limits(limits)
  for child in elf(known[n])['needed']:
   if child not in PLATFORM and child not in seen:
    if child not in known:raise Error('baseline_unavailable',child)
    names.append(child);seen.add(child)
 versions={n:ver for n in names}
 if 'libxxhash.so.0' in names:
  h=ctypes.CDLL(str(known['libxxhash.so.0']));h.XXH_versionNumber.restype=ctypes.c_uint;v=h.XXH_versionNumber();versions['libxxhash.so.0']=f'{v//10000}.{v//100%100}.{v%100}'
 compiler=shutil.which('g++',path='/usr/bin:/bin')
 if not compiler:raise Error('blocked_toolchain','g++')
 compiler=Path(compiler).resolve();checked_copy(Path(__file__).parent/'data/native/baseline.cpp',path/'codec.cpp',limits)
 for header in ('lab_crc.hpp','lab_transforms.hpp'):checked_copy(Path(__file__).parent/'data/native'/header,path/header,limits)
 cid=family+(('-'+str(level)) if family!='stored' else '')+('-dict' if use_dictionary else '')
 cid+=('-'+transform if transform!='identity' else '')+('-static' if link_mode=='static-codec' else '')
 common=['-DCL_TRANSFORM='+str(('identity','delta8','shuffle4').index(transform)),'-std=c++17','-DCL_CODEC='+str(code),'-DCL_LEVEL='+str(level),'-frandom-seed=compression-lab-v1','-ffile-prefix-map=/source=source','-ffile-prefix-map=/output=build']
 commands={op:['{runtime}/codec',op.replace('_','-')]+(['{input_dir}','{output_dir}'] if op.endswith('_dir') else []) for op in ('encode_dir','decode_dir','encode_stream','decode_stream')}
 m={'schema_version':1,'candidate_id':cid,'language':'c++','input_domain':'opaque_bytes','deterministic':True,'threads':1,'source_paths':['codec.cpp','lab_crc.hpp','lab_transforms.hpp'],'artifact_paths':[],'runtime_paths':[],
 'build_commands':[['g++','-O3','-DNDEBUG',*common,'{source}/codec.cpp',*link,'-o','{build}/codec']],
 'sanitizer_build_commands':[['g++','-O1','-g','-fsanitize=address,undefined','-fno-omit-frame-pointer','-no-pie',*common,'{source}/codec.cpp',*link,'-o','{build}/codec']],
 'build_output_paths':['codec'],'executable':'codec','commands':commands,'toolchain':{'g++':{'path':str(compiler),'sha256':checked_sha(compiler,limits)}},
 'dependencies':[{'soname':n,'build_path':str(known[n]),'sha256':checked_sha(known[n],limits),'version':versions[n],'license':'BSD-2-Clause' if n=='libxxhash.so.0' else {'zstd':'BSD-3-Clause','lz4':'BSD-2-Clause','brotli':'MIT','xz':'0BSD'}.get(family,'MIT')} for n in names],
 'hypothesis':{'kind':'conventional-control','statement':f'{family} native {ver}, level {level}; common 32-byte integrity envelope and literal fallback','binding':'No Python codec binding in timing','transform':transform,'link_mode':link_mode,'wire_format':'CLB1-v1' if transform=='identity' else 'CLT1-v1'}}
 # Redistribution notices are real shipped fixed files and are charged, not waived.
 checked_copy(Path(__file__).parent/'data/LICENSE',path/'LICENSE',limits);m['source_paths'].append('LICENSE')
 doc={'zstd':'libzstd1','lz4':'liblz4-1','brotli':'libbrotli1','xz':'liblzma5'}.get(family)
 if doc:
  notice=Path('/usr/share/doc')/doc/'copyright'
  if not notice.is_file():raise Error('dependency_license_missing',str(notice))
  (path/'licenses').mkdir();target='licenses/'+doc+'.copyright';checked_copy(notice,path/target,limits);m['runtime_paths'].append(target);m['training']={'kind':'data-independent'}
 if 'libxxhash.so.0' in names:
  notice=Path('/usr/share/doc/libxxhash0/copyright')
  if not notice.is_file():raise Error('dependency_license_missing',str(notice))
  (path/'licenses').mkdir(exist_ok=True);target='licenses/libxxhash0.copyright';checked_copy(notice,path/target,limits);m['runtime_paths'].append(target);m['training']={'kind':'data-independent'}
 if use_dictionary:
  if family!='zstd':raise Error('unsupported_dictionary')
  b,provenance=dictionary(rows,limits=limits,fitting_partition=fitting_partition);check_limits(limits);(path/'dictionary.bin').write_bytes(b);m['artifact_paths']=['dictionary.bin'];m['training']=provenance
  for cmd in m['commands'].values():cmd.append('{runtime}/dictionary.bin')
 if link_mode=='static-codec':
  static=known[names[0]].parent/('lib'+family+'.a')
  header=Path('/usr/include')/(family+'.h')
  if not static.is_file() or not header.is_file():raise Error('static_codec_unavailable',str(static))
  (path/'vendor').mkdir();assets=[]
  for asset in (static,header):
   target='vendor/'+asset.name;checked_copy(asset,path/target,limits);m['source_paths'].append(target)
   assets.append({'path':target,'sha256':checked_sha(asset,limits),'bytes':asset.stat().st_size,'origin':str(asset)})
  receipt={'schema_version':1,'kind':'precompiled-native-build-assets','family':family,'installed_version':ver,'version_observation':'Shared host library API; static asset identity is its hash, not an independently queried version.',
   'assets':assets,'source_rebuild':'See dependencies/upstream-lock.json and tools/fetch_upstream.py in the lab package; these .a bytes are NOT advertised as upstream source.',
   'accounting':'Complete assets charged as packed source/config; linked code charged in executable; actual dynamic ELF closure still charged.'}
  save(path/'build-assets.json',receipt);m['source_paths'].append('build-assets.json')
  for key in ('build_commands','sanitizer_build_commands'):
   m[key][0]=[a for a in m[key][0] if a not in link]
   index=m[key][0].index('{source}/codec.cpp');m[key][0][index:index]=['-I{source}/vendor']
   index=m[key][0].index('-o');m[key][0][index:index]=['{source}/vendor/'+static.name]
  m['dependencies']=[]  # Embedded codec is not an assumed external runtime; closure still fails on any undeclared .so.
 if offline_replay:
  m.update(schema_version=2,diagnostics={'kind':'cxx-asan-ubsan-v1'},
           decoder={key:list(m[key]) if isinstance(m[key],list) else m[key]
                    for key in ('executable','build_output_paths','artifact_paths','runtime_paths')})
  if use_dictionary:
   from .native_baselines import add_offline_dictionary
   add_offline_dictionary(path,m,'prefix',[],
       ['{source}/vendor/libzstd.a'] if link_mode=='static-codec' else ['-lzstd'],limits)
 check_limits(limits);save(path/'candidate.json',m);return path


def recipes():
 """Explicit factory recipes; names are discovery keys, never evaluation cache keys."""
 result={}
 for family,level,dictionary in MATRIX+[('zstd',-3,False)]:
  name=family+(('-'+str(level)) if family!='stored' else '')+('-dict' if dictionary else '')
  result[name]={'family':family,'level':level,'use_dictionary':dictionary,'transform':'identity','link_mode':'shared','kind':'conventional-control'}
 for family,level in (('zstd',1),('lz4',0)):
  name=family+'-'+str(level)
  for transform in ('delta8','shuffle4'):
   result[name+'-'+transform]={**result[name],'transform':transform,'kind':'compositional-control'}
  result[name+'-static']={**result[name],'link_mode':'static-codec'}
 from .native_baselines import recipes as native_recipes
 result.update(native_recipes())
 return result

def declared_morning_recipes():
 from .native_baselines import recipes as native_recipes
 return list(native_recipes())
