// SPDX-License-Identifier: MIT
// Owned by the parallel native baseline. Main participates; no codec subprocesses.
#pragma once
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <exception>
#include <functional>
#include <mutex>
#include <thread>
#include <vector>

namespace cl_parallel {

// One bounded work cursor rather than a queue containing every chunk. The caller
// is worker zero. Persistent background workers own stable, distinct worker IDs.
class Workers {
public:
    explicit Workers(std::size_t count) {
        try {
            threads_.reserve(count > 0 ? count - 1 : 0);
            for (std::size_t id = 1; id < count; ++id)
                threads_.emplace_back([this, id] { background(id); });
        } catch (...) {
            stop();
            throw;
        }
    }
    Workers(const Workers&) = delete;
    Workers& operator=(const Workers&) = delete;
    ~Workers() { stop(); }

    // Only the main thread calls run(). All jobs finish or cancel before return,
    // including exceptional return. Thus callers may safely reclaim buffers.
    void run(std::size_t jobs, std::function<void(std::size_t, std::size_t)> task) {
        if (jobs == 0) return;
        {
            std::lock_guard<std::mutex> lock(mutex_);
            task_ = std::move(task);
            jobs_ = jobs;
            next_.store(0, std::memory_order_relaxed);
            cancelled_.store(false, std::memory_order_relaxed);
            failure_ = nullptr;
            completed_ = 0;
            ++generation_;
        }
        wake_.notify_all();
        drain(0);
        std::unique_lock<std::mutex> lock(mutex_);
        done_.wait(lock, [this] { return completed_ == threads_.size(); });
        task_ = nullptr;
        if (failure_) std::rethrow_exception(failure_);
    }

private:
    void drain(std::size_t id) noexcept {
        try {
            while (!cancelled_.load(std::memory_order_relaxed)) {
                const auto index = next_.fetch_add(1, std::memory_order_relaxed);
                if (index >= jobs_) break;
                task_(index, id);
            }
        } catch (...) {
            std::lock_guard<std::mutex> lock(mutex_);
            if (!failure_) failure_ = std::current_exception();
            cancelled_.store(true, std::memory_order_relaxed);
        }
    }
    void background(std::size_t id) noexcept {
        std::size_t seen = 0;
        for (;;) {
            {
                std::unique_lock<std::mutex> lock(mutex_);
                wake_.wait(lock, [this, seen] { return stopping_ || generation_ != seen; });
                if (stopping_) return;
                seen = generation_;
            }
            drain(id);
            {
                std::lock_guard<std::mutex> lock(mutex_);
                ++completed_;
            }
            done_.notify_one();
        }
    }
    void stop() noexcept {
        {
            std::lock_guard<std::mutex> lock(mutex_);
            stopping_ = true;
        }
        wake_.notify_all();
        for (auto& thread : threads_) if (thread.joinable()) thread.join();
    }

    std::vector<std::thread> threads_;
    std::mutex mutex_;
    std::condition_variable wake_, done_;
    std::function<void(std::size_t, std::size_t)> task_;
    std::atomic<std::size_t> next_{0};
    std::atomic<bool> cancelled_{false};
    std::size_t generation_ = 0, completed_ = 0, jobs_ = 0;
    bool stopping_ = false;
    std::exception_ptr failure_;
};
}  // namespace cl_parallel
