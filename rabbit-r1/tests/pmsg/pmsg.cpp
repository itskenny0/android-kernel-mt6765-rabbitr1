// SPDX-License-Identifier: Apache-2.0
// The runner links actual pinned liblog sources; syscall wrappers never forward I/O.
#include <atomic>
#include <barrier>
#include <cassert>
#include <cerrno>
#include <cstring>
#include <deque>
#include <fcntl.h>
#include <iostream>
#include <map>
#include <mutex>
#include <string>
#include <thread>
#include <vector>
#include <sys/uio.h>
#include <log/pmsg_writer.h>
#include <private/android_logger.h>
#include "logger.h"
#include "pmsg_reader.h"

struct Open { std::string path; int fd; int error; };
struct File { std::vector<unsigned char> bytes; size_t offset = 0; bool writer = false; };
static std::mutex lock;
static std::deque<Open> plan;
static std::map<int, File> descriptors;
static std::vector<unsigned char> ramoops, blk, captured;
static std::vector<std::string> opened;
static int writer_error, interrupted, next_fd = 10, clock_error, read_error, write_error;
static bool zero_first;
static unsigned writer_error_at;
static unsigned clocks, writes, cases;
static std::atomic<int64_t> now_ns = 10000000000LL;
static constexpr const char* ram_path = "/sys/fs/pstore/pmsg-ramoops-0";
static constexpr const char* blk_path = "/sys/fs/pstore/pmsg-pstore_blk-0";

static void require(bool ok, const char* what) {
  if (!ok) { std::cerr << "FAIL case " << cases << ": " << what << '\n'; std::abort(); }
}

extern "C" int __wrap_open(const char* path, int flags, ...) {
  std::lock_guard guard(lock);
  opened.emplace_back(path);
  if (std::string(path) == "/dev/pmsg0") {
    require(flags == (O_WRONLY | O_CLOEXEC), "writer flags");
    if (interrupted) { --interrupted; errno = EINTR; return -1; }
    if (writer_error && (!writer_error_at || opened.size() == writer_error_at)) { errno = writer_error; return -1; }
    int fd = zero_first ? 0 : next_fd++;
    zero_first = false;
    descriptors[fd].writer = true;
    return fd;
  }
  require(flags == (O_RDONLY | O_CLOEXEC), "reader flags");
  require(!plan.empty(), "unexpected reader open");
  Open selected = plan.front(); plan.pop_front();
  require(selected.path == path, "reader backend/open order");
  if (selected.fd < 0) { errno = selected.error; return -1; }
  require(selected.path == ram_path || selected.path == blk_path, "fixed reader path");
  descriptors[selected.fd] = {selected.path == ram_path ? ramoops : blk, 0, false};
  return selected.fd;
}

extern "C" int __wrap_close(int fd) {
  std::lock_guard guard(lock);
  require(descriptors.erase(fd) == 1, "close only owned descriptor");
  errno = EIO; // Successful close must not obscure the preceding open error.
  return 0;
}

extern "C" int __wrap_clock_gettime(clockid_t id, timespec* value) {
  require(id == CLOCK_MONOTONIC, "retry clock must be monotonic");
  std::lock_guard guard(lock);
  ++clocks;
  if (clock_error) { errno = clock_error; return -1; }
  auto now = now_ns.load();
  *value = {now / 1000000000LL, now % 1000000000LL};
  return 0;
}

extern "C" ssize_t __wrap_writev(int fd, const iovec* vec, int count) {
  std::lock_guard guard(lock);
  require(descriptors.contains(fd) && descriptors[fd].writer, "write only pmsg descriptor");
  if (write_error) { errno = write_error; return -1; }
  ++writes;
  captured.clear();
  for (int i = 0; i < count; ++i) {
    const auto* start = static_cast<const unsigned char*>(vec[i].iov_base);
    captured.insert(captured.end(), start, start + vec[i].iov_len);
  }
  return captured.size();
}

extern "C" ssize_t __wrap_read(int fd, void* data, size_t count) {
  std::lock_guard guard(lock);
  require(descriptors.contains(fd) && !descriptors[fd].writer, "read only selected pstore file");
  if (read_error) { errno = read_error; return -1; }
  auto& file = descriptors[fd];
  count = std::min(count, file.bytes.size() - file.offset);
  if (count) std::memcpy(data, file.bytes.data() + file.offset, count);
  file.offset += count;
  return count;
}

extern "C" off_t __wrap_lseek(int fd, off_t offset, int whence) {
  std::lock_guard guard(lock);
  require(descriptors.contains(fd) && !descriptors[fd].writer, "seek only pstore descriptor");
  require(whence == SEEK_CUR, "seek mode");
  auto& file = descriptors[fd];
  require(offset >= 0 && file.offset + offset <= file.bytes.size(), "seek bounds");
  file.offset += offset;
  return file.offset;
}

static void reset() {
  PmsgClose();
  require(descriptors.empty(), "descriptor leak");
  plan.clear(); ramoops.clear(); blk.clear(); captured.clear(); opened.clear();
  writer_error = interrupted = clock_error = read_error = write_error = 0;
  next_fd = 10; zero_first = false; writer_error_at = clocks = writes = 0; now_ns = 10000000000LL;
  ++cases;
}

static int write_message(log_id_t id = LOG_ID_SECURITY) {
  static char payload[] = {ANDROID_LOG_INFO, 't', 'a', 'g', 0, 'm', 's', 'g', 0};
  iovec vec{payload, sizeof(payload)};
  timespec timestamp{100, 123456};
  return PmsgWrite(id, &timestamp, &vec, 1, 1000, 123, 456);
}

static std::vector<unsigned char> packet() {
  reset();
  require(write_message() > 0, "writer builds packet");
  auto result = captured;
  require(result.size() == sizeof(android_pmsg_log_header_t) + sizeof(android_log_header_t) + 9,
          "serialized packet size");
  PmsgClose();
  return result;
}

static void check_read(const std::vector<unsigned char>& payload) {
  logger_list list{}; list.log_mask = ~0U;
  log_msg message{};
  int ret = PmsgRead(&list, &message);
  require(ret > 0, "read a record");
  require(message.entry.uid == 1000 && message.entry.pid == 123 && message.entry.tid == 456,
          "read preserves identity");
  require(message.entry.sec == 100 && message.entry.nsec == 123456, "read preserves timestamp");
  require(message.entry.lid == LOG_ID_SECURITY && message.entry.len == 9, "read preserves header");
  require(std::memcmp(message.msg(), payload.data() + sizeof(android_pmsg_log_header_t) +
                      sizeof(android_log_header_t), 9) == 0, "read preserves payload");
  require(PmsgRead(&list, &message) == -EAGAIN, "EOF stays on selected backend");
  PmsgClose(&list);
  require(plan.empty(), "planned opens completed");
}

int main() {
  auto record = packet();
  reset(); ramoops = record; blk = record;
  plan.push_back({ram_path, 10, 0}); check_read(record); // Both present: first only.
  require(opened.size() == 1, "no duplicate backend consumption");

  reset(); blk = record;
  plan = {{ram_path, -1, ENOENT}, {blk_path, 10, 0}}; check_read(record);
  for (int error : {EACCES, EPERM, EIO}) {
    reset(); plan = {{ram_path, -1, error}};
    logger_list list{}; list.log_mask = ~0U; log_msg msg{};
    require(PmsgRead(&list, &msg) == -error, "primary error is not hidden");
    require(opened.size() == 1, "no fallback on non-ENOENT");
  }
  for (int error : {ENOENT, EACCES, EIO}) {
    reset(); plan = {{ram_path, -1, ENOENT}, {blk_path, -1, error}};
    logger_list list{}; list.log_mask = ~0U; log_msg msg{};
    require(PmsgRead(&list, &msg) == -error, "fallback preserves error");
  }
  for (bool fallback : {false, true}) {
    reset(); ramoops = blk = record;
    if (fallback) plan.push_back({ram_path, -1, ENOENT});
    auto path = fallback ? blk_path : ram_path;
    plan.push_back({path, 0, 0}); plan.push_back({path, 10, 0});
    check_read(record);
    reset();
    if (fallback) plan.push_back({ram_path, -1, ENOENT});
    plan.push_back({path, 0, 0}); plan.push_back({path, -1, ENOENT});
    logger_list list{}; list.log_mask = ~0U; log_msg msg{};
    require(PmsgRead(&list, &msg) == -ENOENT, "fd zero reopen preserves errno");
    require(descriptors.empty(), "fd zero closed on reopen failure");
  }
  for (size_t length : {size_t(0), size_t(4), record.size() - 1}) {
    reset(); ramoops = record; ramoops.resize(length); blk = record;
    plan = {{ram_path, 10, 0}};
    logger_list list{}; list.log_mask = ~0U; log_msg msg{};
    require(PmsgRead(&list, &msg) == (length ? -EIO : -EAGAIN), "empty/truncated record behavior");
    PmsgClose(&list);
    require(opened.size() == 1, "corrupt/empty primary does not select another history");
  }
  reset(); blk = {0xaa, 0xbb, 0xcc}; blk.insert(blk.end(), record.begin(), record.end());
  plan = {{ram_path, -1, ENOENT}, {blk_path, 10, 0}}; check_read(record);
  reset(); blk = record; plan = {{ram_path, -1, ENOENT}, {blk_path, 10, 0}};
  logger_list list{}; list.log_mask = 1U << LOG_ID_MAIN; log_msg msg{};
  require(PmsgRead(&list, &msg) == -EAGAIN, "existing log mask still applies"); PmsgClose(&list);
  reset(); read_error = EIO; plan = {{ram_path, 10, 0}};
  list.fd = 0; list.log_mask = ~0U;
  require(PmsgRead(&list, &msg) == -EIO, "read error propagated"); PmsgClose(&list);

  reset(); writer_error = ENOENT;
  for (int i = 0; i < 1000; ++i) require(write_message() == -EBADF, "pending backend");
  require(opened.size() == 1, "missing backend open attempts bounded");
  writer_error = 0; now_ns = 10999999999LL;
  require(write_message() == -EBADF && opened.size() == 1, "retry not early");
  now_ns = 11000000000LL;
  require(write_message() > 0 && opened.size() == 2, "late backend begins receiving logs");
  auto old_clocks = clocks;
  for (int i = 0; i < 1000; ++i) require(write_message() > 0, "cached fd writes");
  require(opened.size() == 2 && clocks == old_clocks, "cached success has no retry overhead");

  reset(); writer_error = ENOENT;
  for (int second = 0; second < 5; ++second) {
    now_ns = (10LL + second) * 1000000000LL;
    for (int i = 0; i < 100; ++i) require(write_message() == -EBADF, "absent device stays bounded");
  }
  require(opened.size() == 5, "one attempt per monotonic second");
  for (int error : {EACCES, ENODEV, EMFILE, EPERM}) {
    reset(); writer_error = error;
    require(write_message() == -EBADF, "permanent open failure");
    writer_error = 0; now_ns = 90000000000LL;
    require(write_message() == -EBADF && opened.size() == 1, "permanent failure stays cached");
    PmsgClose(); require(write_message() > 0, "explicit close resets state and retry timer");
  }
  reset(); interrupted = 2;
  require(write_message() > 0 && opened.size() == 3, "EINTR retry retained");
  reset(); zero_first = true;
  require(write_message() > 0 && opened.size() == 2 && !descriptors.contains(0), "writer fd zero reopens");
  reset(); zero_first = true; writer_error = ENOENT; writer_error_at = 2;
  require(write_message() == -EBADF && descriptors.empty(), "writer fd zero reopen missing");
  writer_error = 0; now_ns = 11000000000LL;
  require(write_message() > 0 && opened.size() == 3, "reopen ENOENT remains retryable after close");
  reset(); clock_error = EINVAL;
  require(write_message() == -EBADF && opened.empty(), "clock failure makes no unbounded attempt");
  clock_error = 0; require(write_message() > 0, "clock failure is recoverable");
  reset(); write_error = ENOSPC;
  require(write_message() == -ENOSPC, "write error preserved");
  write_error = 0; require(write_message() > 0 && opened.size() == 1, "write error keeps existing fd");

  reset(); writer_error = ENOENT;
  constexpr int threads = 32;
  auto parallel = [] {
    std::barrier start(threads);
    std::vector<std::thread> workers;
    for (int i = 0; i < threads; ++i) workers.emplace_back([&] { start.arrive_and_wait();
      for (int j = 0; j < 100; ++j) write_message(); });
    for (auto& worker : workers) worker.join();
  };
  parallel(); require(opened.size() == 1, "concurrent absence has one open attempt");
  writer_error = 0; now_ns = 11000000000LL;
  parallel();
  require(opened.size() == 2 && descriptors.size() == 1 && writes > 0,
          "concurrent recovery retains one valid fd");
  reset();
#if ANDROID_DEBUGGABLE
  require(write_message(LOG_ID_MAIN) > 0, "debuggable main buffer remains enabled");
#else
  require(write_message(LOG_ID_MAIN) == -1 && opened.empty(), "user main buffer remains excluded");
  require(write_message(LOG_ID_EVENTS) == -EPERM && opened.empty(), "user non-SNET event excluded");
  require(write_message(LOG_ID_SECURITY) > 0, "user security buffer remains enabled");
#endif
  reset();
  std::cout << "PASS: " << cases << " production reader/writer scenarios (debuggable="
            << ANDROID_DEBUGGABLE << "), including concurrent retry and 3,200-message waves\n";
}
