"""ASan exception support exposes only the diagnostic process's live maps."""
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from compression_lab import candidate, runner
from compression_lab.util import sha


SOURCE = r'''
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <dirent.h>
#include <fcntl.h>
#include <stdexcept>
#include <sys/statvfs.h>
#include <unistd.h>
#include <vector>
__attribute__((noinline)) void reject() { throw std::runtime_error("expected rejection"); }
__attribute__((noinline)) int stack_bug(int i) { volatile char bytes[8]{}; bytes[i]=7; return bytes[0]; }
__attribute__((noinline)) char* allocate(int n) { return new char[n]{}; }
__attribute__((noinline)) int heap_bug(int i) { volatile char* bytes=allocate(8); bytes[i]=7; int value=bytes[0]; delete[] bytes; return value; }
bool only_entry(const char* path, const char* expected) {
    DIR* dir=opendir(path); if (!dir) return false;
    int count=0; bool good=true;
    while (auto* entry=readdir(dir)) {
        if (!strcmp(entry->d_name,".") || !strcmp(entry->d_name,"..")) continue;
        ++count; if (!expected || strcmp(entry->d_name,expected)) good=false;
    }
    closedir(dir); return good && count==(expected?1:0);
}
int main(int argc,char** argv) {
    if (argc<2) return 90;
    if (!strcmp(argv[1],"throw")) {
        try { std::vector<char> valid(32,7); reject(); return valid[0]; }
        catch (const std::exception& error) { fprintf(stderr,"%s\n",error.what()); return 2; }
    }
    if (!strcmp(argv[1],"stack")) return stack_bug(atoi(argv[2]));
    if (!strcmp(argv[1],"heap")) return heap_bug(atoi(argv[2]));
    bool diagnostic=!strcmp(argv[1],"diagnostic");
    if (!only_entry("/proc",diagnostic?"self":nullptr)) return 91;
    if (diagnostic && !only_entry("/proc/self","maps")) return 92;
    for (const char* path:{"/proc/1/environ","/proc/self/environ","/proc/self/fd/0",
         "/proc/self/root","/proc/self/exe","/proc/2/maps","/.diagnostic-proc"})
        if (access(path,F_OK)==0) return 93;
    if (argc>2 && access(argv[2],F_OK)==0) return 94;
    FILE* file=fopen("/proc/self/maps","r");
    if (!diagnostic) return file?95:0;
    if (!file) return 96;
    char line[4096]; bool current_stack=false, current_binary=false;
    unsigned long here=reinterpret_cast<unsigned long>(&file);
    while (fgets(line,sizeof(line),file)) {
        unsigned long start=0,end=0;
        if (sscanf(line,"%lx-%lx",&start,&end)==2 && start<=here && here<end) current_stack=true;
        if (strstr(line,"/candidate/codec")) current_binary=true;
    }
    fclose(file);
    struct statvfs mount{};
    if (statvfs("/proc/self/maps",&mount) || !(mount.f_flag&ST_RDONLY)) return 97;
    int fd=open("/proc/self/maps",O_WRONLY); if (fd>=0) { close(fd); return 98; }
    if (!current_stack || !current_binary) return 99;
    puts("only current process maps visible, live after exec and read-only"); return 0;
}
'''


class SanitizerProcessMaps(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory(prefix='clab-sanitizer-maps-')
        cls.root=Path(cls.temp.name); cls.source=cls.root/'source'; cls.source.mkdir()
        (cls.source/'codec.cpp').write_text(SOURCE)
        compiler=Path(shutil.which('g++',path='/usr/bin:/bin')).resolve()
        tail=['{source}/codec.cpp','-o','{build}/codec']
        cls.manifest={'schema_version':2,'language':'c++','threads':1,
            'toolchain':{'g++':{'path':str(compiler),'sha256':sha(compiler)}},'dependencies':[],
            'build_commands':[['g++','-std=c++17','-O2',*tail]],
            'sanitizer_build_commands':[['g++','-std=c++17','-O1','-g','-no-pie','-fsanitize=address,undefined','-fno-omit-frame-pointer',*tail]],
            'build_output_paths':['codec'],'executable':'codec','artifact_paths':[],'runtime_paths':[],
            'decoder':{'executable':'codec','build_output_paths':['codec'],'artifact_paths':[],'runtime_paths':[]},
            'diagnostics':{'kind':'cxx-asan-ubsan-v1'}}
        cls.builds={}
        for diagnostic in (False,True):
            path=cls.root/('diagnostic' if diagnostic else 'normal')
            build=candidate.build(cls.source,cls.manifest,path,sanitizer=diagnostic)
            cls.builds[diagnostic]=(path,build)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def call(self,*args,diagnostic=True):
        path,build=self.builds[diagnostic]
        return runner.execute(['/candidate/codec',*args],output=self.root/'output',
            readonly={'/candidate':path},sanitizer=diagnostic,
            runtime_files=candidate.runtime_mounts(build['decoder_dependencies'],diagnostic),check=False,timeout=5)

    def test_expected_exception_has_no_sanitizer_false_positive(self):
        result=self.call('throw')
        self.assertEqual(result['returncode'],2)
        self.assertEqual(result['stdout'],'')
        self.assertIn('expected rejection',result['stderr'])
        self.assertNotIn('AddressSanitizer',result['stderr'])
        self.assertNotIn('runtime error:',result['stderr'])

    def test_only_own_live_readonly_maps_are_visible_in_diagnostics(self):
        result=self.call('diagnostic','/proc/'+str(os.getpid())+'/maps')
        self.assertEqual(result['returncode'],0,result['stderr'])
        self.assertIn('live after exec',result['stdout'])

    def test_normal_runtime_still_has_no_proc_contents(self):
        result=self.call('normal','/proc/'+str(os.getpid())+'/maps',diagnostic=False)
        self.assertEqual(result['returncode'],0,result['stderr'])

    def test_actual_stack_and_heap_overflows_still_fail(self):
        for kind in ('stack','heap'):
            with self.subTest(kind=kind):
                result=self.call(kind,'24')
                self.assertNotEqual(result['returncode'],0)
                if kind=='heap':self.assertIn('heap-buffer-overflow',result['stderr'])
                else:self.assertTrue('stack-buffer-overflow' in result['stderr'] or 'runtime error: index 24 out of bounds' in result['stderr'],result['stderr'])
                self.assertEqual(result['stdout'],'')


if __name__=='__main__':
    unittest.main()
