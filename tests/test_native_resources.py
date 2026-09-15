"""Small real pthread/security canaries, not compression performance claims."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from compression_lab import runner
from compression_lab.util import Error


CANARY = r'''
#include <atomic>
#include <cerrno>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <pthread.h>
#include <sched.h>
#include <signal.h>
#include <sys/mman.h>
#include <sys/resource.h>
#include <sys/socket.h>
#include <sys/syscall.h>
#include <sys/wait.h>
#include <unistd.h>

extern "C" const char *__asan_default_options() {
    return "detect_leaks=0:symbolize=0:abort_on_error=1:allocator_may_return_null=1";
}
extern "C" const char *__ubsan_default_options() {
    return "halt_on_error=1:print_stacktrace=0";
}

std::atomic<int> ready{0}, release_workers{0}, bad_affinity{0};
cpu_set_t initial;
pid_t main_pid;
void* worker(void*) {
    cpu_set_t actual; CPU_ZERO(&actual);
    if (sched_getaffinity(0, sizeof(actual), &actual) ||
        !CPU_EQUAL(&actual, &initial) || getpid() != main_pid) ++bad_affinity;
    ++ready;
    while (!release_workers.load()) usleep(1000);
    return nullptr;
}
void* memory_worker(void* arg) {
    void* p = mmap(nullptr, 80u<<20, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
    *static_cast<int*>(arg) = p == MAP_FAILED && errno == ENOMEM;
    if (p != MAP_FAILED) munmap(p, 80u<<20);
    return nullptr;
}
int main(int argc, char** argv) {
    int n = atoi(getenv("COMPRESSION_LAB_THREADS"));
    CPU_ZERO(&initial); sched_getaffinity(0, sizeof(initial), &initial); main_pid = getpid();
    if (argc > 1 && !strcmp(argv[1], "recycle")) {
        for (int round = 0; round < 100; ++round) {
            ready = 0; release_workers = 0;
            pthread_t pool[64]; int created = 0, error = 0;
            for (; created < n - 1; ++created) {
                error = pthread_create(&pool[created], nullptr, worker, nullptr);
                if (error) break;
            }
            while (ready.load() < created) sched_yield();
            release_workers = 1;
            for (int i = 0; i < created; ++i) pthread_join(pool[i], nullptr);
            if (error) {
                printf("{\"round\":%d,\"created\":%d,\"error\":%d}\n", round, created, error);
                return 73;
            }
        }
        printf("{\"completed_pools\":100,\"bad_affinity\":%d}\n", bad_affinity.load());
        return bad_affinity ? 74 : 0;
    }
    if (argc > 1 && !strcmp(argv[1], "memory")) {
        void* p = mmap(nullptr, 80u<<20, PROT_READ|PROT_WRITE, MAP_PRIVATE|MAP_ANONYMOUS, -1, 0);
        if (p == MAP_FAILED) return 70;
        memset(p, 7, 80u<<20);
        pthread_t t; int aggregate = 0;
        int rc = pthread_create(&t, nullptr, memory_worker, &aggregate);
        if (rc) return 71;
        pthread_join(t, nullptr); usleep(120000);
        printf("{\"aggregate_address_space_limit\":%d}\n", aggregate);
        return aggregate ? 0 : 72;
    }
    bool single = argc > 1 && !strcmp(argv[1], "single");
    bool excessive = argc > 1 && !strcmp(argv[1], "excessive");
    pthread_t workers[64]; int created = 0, last_error = 0;
    int requested = single ? 1 : excessive ? n + 1 : n - 1;
    for (int i = 0; i < requested; ++i) {
        int rc = pthread_create(&workers[created], nullptr, worker, nullptr);
        if (rc) { last_error = rc; break; }
        ++created;
    }
    while (ready.load() < created) usleep(1000);
    usleep(150000);
    release_workers = 1;
    for (int i = 0; i < created; ++i) pthread_join(workers[i], nullptr);
    errno = 0; pid_t child = fork(); int fork_errno = errno;
    if (child == 0) _exit(0);
    if (child > 0) waitpid(child, nullptr, 0);
    errno = 0; long raw = syscall(SYS_clone, SIGCHLD, nullptr, nullptr, nullptr, 0); int clone_errno = errno;
    if (raw == 0) _exit(0);
    if (raw > 0) waitpid(raw, nullptr, 0);
    errno = 0; long c3 = syscall(SYS_clone3, nullptr, 0); int clone3_errno = errno;
    errno = 0; int sock = socket(AF_INET, SOCK_STREAM, 0); int socket_errno = errno;
    if (sock >= 0) close(sock);
    errno = 0; int ns = unshare(CLONE_NEWUSER); int namespace_errno = errno;
    errno = 0; int affinity = sched_setaffinity(0, sizeof(initial), &initial); int affinity_errno = errno;
    struct rlimit cpu, as, tasks;
    getrlimit(RLIMIT_CPU, &cpu); getrlimit(RLIMIT_AS, &as); getrlimit(RLIMIT_NPROC, &tasks);
    printf("{\"created_workers\":%d,\"thread_error\":%d,\"bad_affinity\":%d,"
           "\"fork_denied\":%d,\"clone_process_denied\":%d,\"clone3_errno\":%d,"
           "\"network_denied\":%d,\"namespace_denied\":%d,\"affinity_change_denied\":%d,"
           "\"threads_env\":%d,\"omp_env\":%d,\"cpu_limit\":%llu,\"as_limit\":%llu,\"nproc_limit\":%llu,\"cpus\":[",
           created, last_error, bad_affinity.load(), child < 0 && fork_errno == EPERM,
           raw < 0 && clone_errno == EPERM, c3 < 0 ? clone3_errno : 0,
           sock < 0 && socket_errno == EPERM, ns < 0 && namespace_errno == EPERM,
           affinity < 0 && affinity_errno == EPERM, n, atoi(getenv("OMP_NUM_THREADS")),
           (unsigned long long)cpu.rlim_cur, (unsigned long long)as.rlim_cur, (unsigned long long)tasks.rlim_cur);
    bool comma = false;
    for (int c = 0; c < CPU_SETSIZE; ++c) if (CPU_ISSET(c, &initial)) { printf("%s%d", comma ? "," : "", c); comma = true; }
    puts("]}");
}
'''


class ResourceValidation(unittest.TestCase):
    def test_reject_invalid_or_unavailable_allocations(self):
        for threads in (True, 0, 65, 1.0, '4'):
            with self.subTest(threads=threads), self.assertRaises(Error):
                runner.resolve_resources(threads)
        for cpus in ([], [True], [-1], [0, 0], '4,6'):
            with self.subTest(cpus=cpus), self.assertRaises(Error):
                runner.resolve_resources(4, cpus)
        with self.assertRaises(Error) as caught:
            runner.resolve_resources(4, [max(os.sched_getaffinity(0)) + 1])
        self.assertEqual(caught.exception.code, 'unavailable_cpus')

    def test_omitted_cpus_choose_physical_cores_before_siblings(self):
        topology = [dict(cpu=c, physical_package_id=0, core_id=c//2, thread_siblings_list=f'{c//2*2}-{c//2*2+1}') for c in range(6)]
        with patch('compression_lab.resources.os.sched_getaffinity', return_value=set(range(6))), \
             patch('compression_lab.resources.cpu_topology', side_effect=lambda cpus: [row for row in topology if row['cpu'] in cpus]):
            self.assertEqual(runner.resolve_resources(3)['cpus'], [0, 2, 4])
            self.assertEqual(runner.resolve_resources(64)['cpus'], list(range(6)))


class NativeResources(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(prefix='clab-resource-tests-')
        cls.root = Path(cls.tmp.name)
        source = cls.root / 'canary.cpp'
        source.write_text(CANARY)
        subprocess.run(['g++', '-std=c++17', '-O2', '-pthread', str(source), '-o', str(cls.root/'canary')], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def call(self, *args, **options):
        return runner.execute(['/candidate/canary', *args], output=self.root/'out',
                              readonly={'/candidate':self.root}, timeout=3, **options)

    def test_four_native_threads_and_denial_boundaries(self):
        cpus = [4, 6, 8, 10] if set([4, 6, 8, 10]) <= os.sched_getaffinity(0) else runner.resolve_resources(4)['cpus']
        result = self.call(threads=4, cpus=cpus)
        native = json.loads(result['stdout'])
        self.assertEqual(native['created_workers'], 3, native)
        self.assertEqual(native['bad_affinity'], 0)
        self.assertEqual(native['cpus'], cpus)
        self.assertEqual(native['threads_env'], 4)
        self.assertEqual(native['omp_env'], 4)
        for name in ('fork_denied', 'clone_process_denied', 'network_denied', 'namespace_denied', 'affinity_change_denied'):
            self.assertEqual(native[name], 1, native)
        self.assertEqual(native['clone3_errno'], 38) # ENOSYS fallback actually exercised
        self.assertEqual(native['cpu_limit'], result['resources']['rlimit_cpu_seconds'])
        self.assertEqual(native['as_limit'], 2*1024**3)
        self.assertEqual(result['max_observed_native_tasks'], 4)
        self.assertEqual(result['max_observed_threads'], 4)
        self.assertEqual(result['max_observed_native_processes'], 1)
        self.assertGreater(result['cpu_total_seconds'], 0)
        self.assertFalse(result['resources']['enforcement']['dedicated_per_call_cgroup'])

    def test_default_preserves_single_thread_denial(self):
        result = self.call('single')
        native = json.loads(result['stdout'])
        self.assertEqual(native['created_workers'], 0)
        self.assertNotEqual(native['thread_error'], 0)
        self.assertEqual(result['cpus'], [min(os.sched_getaffinity(0))])
        self.assertEqual(result['max_observed_native_tasks'], 1)

    def test_repeated_joined_pools_keep_the_four_thread_allocation(self):
        result = self.call('recycle', threads=4)
        self.assertEqual(json.loads(result['stdout'])['completed_pools'], 100)
        self.assertLessEqual(result['max_observed_native_live_tasks'], 4)

    def test_repeated_joined_pools_under_asan_and_ubsan(self):
        binary = self.root / 'diagnostic-canary'
        subprocess.run(['g++', '-std=c++17', '-O1', '-g', '-pthread',
                        '-fsanitize=address,undefined', '-fno-omit-frame-pointer', '-no-pie',
                        str(self.root/'canary.cpp'), '-o', str(binary)],
                       check=True, capture_output=True)
        result = runner.execute(['/candidate/diagnostic-canary', 'recycle'],
                                output=self.root/'diagnostic-out', readonly={'/candidate':self.root},
                                timeout=5, threads=4, sanitizer=True)
        self.assertEqual(json.loads(result['stdout'])['completed_pools'], 100)
        self.assertNotIn('ERROR:', result['stderr'])
        self.assertLessEqual(result['max_observed_native_live_tasks'], 4)

    def test_shared_memory_budget_is_not_per_thread(self):
        result = self.call('memory', threads=2, memory=128*1024**2)
        self.assertEqual(json.loads(result['stdout'])['aggregate_address_space_limit'], 1)
        self.assertGreater(result['observed_native_peak_rss_bytes'], 80*1024**2)

    def test_excess_tasks_are_denied_or_terminated(self):
        try:
            result = self.call('excessive', threads=4)
        except Error as error:
            self.assertEqual(error.code, 'thread_limit')
            self.assertGreater(error.measurement['max_observed_native_live_tasks'], 4)
        else:
            native = json.loads(result['stdout'])
            self.assertLessEqual(native['created_workers'], 3, native)
            self.assertNotEqual(native['thread_error'], 0)

    def test_polling_terminates_excess_threads_without_kernel_nproc_limit(self):
        with self.assertRaises(Error) as caught:
            self.call('excessive', threads=2, mode='exploratory')
        self.assertEqual(caught.exception.code, 'thread_limit')
        self.assertGreater(caught.exception.measurement['max_observed_native_tasks'], 2)
        self.assertIsNone(caught.exception.measurement['resources']['rlimit_nproc'])


if __name__ == '__main__':
    unittest.main()
