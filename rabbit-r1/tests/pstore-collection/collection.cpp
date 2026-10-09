// SPDX-License-Identifier: Apache-2.0
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

#include <algorithm>
#include <array>
#include <cassert>
#include <cerrno>
#include <chrono>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <string>
#include <thread>
#include <vector>
namespace fs = std::filesystem;
static const std::string root = R1_TEST_DIRECTORY;
static int errors = 0, rotations = 0, pmsg_reads = 0, sleeps = 0,
           clock_calls = 0;
static long long millis = 0;
static std::function<void()> after_sleep;
static std::string fault_path;
static int fault_error = 0;
static std::string selected_pmsg;
static bool emit_pmsg = false;
std::string Map(const std::string& p) {
  if (p.starts_with(root + "/") || p == root) return p;
  for (auto [a, b] : std::vector<std::pair<std::string, std::string>>{
           {"/sys/fs/pstore", "/pstore"},
           {"/data/misc/recovery", "/recovery"},
           {"/cache", "/cache"}})
    if (p == a || p.starts_with(a + "/")) return root + b + p.substr(a.size());
  if (p == "/proc/mounts") return root + "/mounts";
  std::cerr << "Forbidden path: " << p << "\n";
  std::abort();
}
int TestAccess(const char* p, int mode) {
  if (p == fault_path) {
    errno = fault_error;
    return -1;
  }
  return ::access(Map(p).c_str(), mode);
}
int TestStat(const char* p, struct stat* s) {
  return ::stat(Map(p).c_str(), s);
}
int TestOpen(const char* p, int flags) {
  if (p == fault_path) {
    errno = fault_error;
    return -1;
  }
  return ::open(Map(p).c_str(), flags);
}
FILE* TestFopen(const char* p, const char* mode) {
  if (p == fault_path) {
    errno = fault_error;
    return nullptr;
  }
  return ::fopen(Map(p).c_str(), mode);
}
int TestUnlink(const char* p) { return ::unlink(Map(p).c_str()); }
#define access(...) TestAccess(__VA_ARGS__)
#define stat(...) TestStat(__VA_ARGS__)
#define open(...) TestOpen(__VA_ARGS__)
#define fopen(...) TestFopen(__VA_ARGS__)
#define unlink(...) TestUnlink(__VA_ARGS__)
#define main recovery_persist_main
#include R1_RECOVERY_SOURCE
#undef main
#undef access
#undef stat
#undef open
#undef fopen
#undef unlink
std::chrono::steady_clock::time_point TestNow() asm(
    "__wrap__ZNSt6chrono3_V212steady_clock3nowEv");
std::chrono::steady_clock::time_point TestNow() {
  ++clock_calls;
  return std::chrono::steady_clock::time_point(
      std::chrono::milliseconds(millis));
}
extern "C" int __wrap_nanosleep(const struct timespec* requested,
                                struct timespec*) {
  ++sleeps;
  millis += requested->tv_sec * 1000 + requested->tv_nsec / 1000000;
  assert(millis <= 30100);
  if (after_sleep) after_sleep();
  return 0;
}
void Put(const std::string& p, const std::string& content) {
  auto q = Map(p);
  fs::create_directories(fs::path(q).parent_path());
  std::ofstream f(q, std::ios::binary);
  f << content;
  assert(f.good());
}
std::string Get(const std::string& p) {
  std::ifstream f(Map(p), std::ios::binary);
  assert(f);
  return {std::istreambuf_iterator<char>(f), {}};
}
void rotate_logs(const char* log, const char* kmsg) {
  if (rotations++) return;
  for (auto p : {log, kmsg})
    if (fs::exists(Map(p))) fs::rename(Map(p), Map(p) + ".1");
}
static std::function<void()> before_pmsg_open;
ssize_t __android_log_pmsg_file_read_from_backend(const char* backend, log_id_t,
                                                  char, const char*,
                                                  PmsgCallback callback,
                                                  void* arg) {
  ++pmsg_reads;
  if (before_pmsg_open) before_pmsg_open();
  assert(backend);
  // The real parser and explicit-backend entry point have separate compiled
  // tests.
  selected_pmsg = std::string(backend) == "ramoops"
                      ? "/sys/fs/pstore/pmsg-ramoops-0"
                      : "/sys/fs/pstore/pmsg-pstore_blk-0";
  int fd = TestOpen(selected_pmsg.c_str(), O_RDONLY);
  if (fd < 0) return -errno;
  ::close(fd);
  if (emit_pmsg)
    return callback(LOG_ID_SYSTEM, ANDROID_LOG_INFO, "recovery/last_log",
                    "new recovery log", 16, arg);
  return 0;
}
ssize_t __android_log_pmsg_file_read(log_id_t id, char prio, const char* prefix,
                                     PmsgCallback fn, void* arg) {
  return __android_log_pmsg_file_read_from_backend("ramoops", id, prio, prefix,
                                                   fn, arg);
}

void Reset() {
  fs::remove_all(root);
  fs::create_directories(root + "/pstore");
  fs::create_directories(root + "/recovery");
  Put("/proc/mounts", "rootfs / rootfs rw 0 0\n");
  errors = rotations = pmsg_reads = sleeps = clock_calls = 0;
  millis = 0;
  after_sleep = {};
  before_pmsg_open = {};
  fault_path.clear();
  fault_error = 0;
  selected_pmsg.clear();
  emit_pmsg = false;
  rotated = false;
}
int Main() {
  char name[] = "recovery-persist";
  char* args[] = {name, nullptr};
  return recovery_persist_main(1, args);
}
int main() {
  int cases = 0;
  for (size_t n : {size_t(0), size_t(1), size_t(16383), size_t(16384),
                   size_t(16385), size_t(49171)}) {
    Reset();
    std::string x(n, 'x');
    Put(root + "/a", x);
    Put(root + "/b", x);
#ifdef ORIGINAL
    assert(compare_file((root + "/a").c_str(), (root + "/b").c_str()) ==
           (n == 0));
    if (n) assert(errors > 0);
#else
    assert(compare_file((root + "/a").c_str(), (root + "/b").c_str()));
    assert(errors == 0);
#endif
    ++cases;
    if (n) {
      x.back() = 'y';
      Put(root + "/b", x);
      assert(!compare_file((root + "/a").c_str(), (root + "/b").c_str()));
      ++cases;
    }
  }
#ifdef ORIGINAL
  std::cout << "Original-source controls passed: identical nonempty files "
               "reproduce EOF failure ("
            << cases << " cases)\n";
  return 0;
#else
  Reset();
  Put(root + "/a", "x");
  Put(root + "/b", "xx");
  assert(!compare_file((root + "/a").c_str(), (root + "/b").c_str()));
  ++cases;
  Reset();
  Put(root + "/keep", "preserved");
  copy_file((root + "/missing").c_str(), (root + "/keep").c_str());
  assert(Get(root + "/keep") == "preserved" && errors > 0);
  ++cases;
  Reset();
  Put("/sys/fs/pstore/pmsg-ramoops-0", "pmsg");
  Put("/sys/fs/pstore/console-ramoops-0", "ramoops");
  Put("/sys/fs/pstore/pmsg-pstore_blk-0", "other");
  Put("/sys/fs/pstore/console-pstore_blk-0", "other console");
  assert(Main() == 0);
  assert(selected_pmsg == "/sys/fs/pstore/pmsg-ramoops-0");
  assert(Get(LAST_KMSG_FILE) == "ramoops" && sleeps == 0);
  ++cases;
  Reset();
  Put("/sys/fs/pstore/console-ramoops", "legacy");
  Put("/sys/fs/pstore/pmsg-pstore_blk-0", "other");
  Put("/sys/fs/pstore/console-pstore_blk-0", "other console");
  assert(Main() == 0);
  assert(pmsg_reads == 0 && Get(LAST_KMSG_FILE) == "legacy" && sleeps == 0);
  ++cases;
  Reset();
  Put("/sys/fs/pstore/pmsg-ramoops-0", "pmsg");
  Put("/sys/fs/pstore/console-pstore_blk-0", "other console");
  assert(Main() == 0);
  assert(pmsg_reads == 1 && !fs::exists(Map(LAST_KMSG_FILE)) && rotations == 0);
  ++cases;
  Reset();
  Put("/sys/fs/pstore/pmsg-pstore_blk-0", "pmsg");
  Put("/sys/fs/pstore/console-pstore_blk-0", "console");
  Put(LAST_KMSG_FILE, "console");
  assert(Main() == 0);
  assert(rotations == 0 && Get(LAST_KMSG_FILE) == "console");
  ++cases;
  Reset();
  Put("/sys/fs/pstore/pmsg-pstore_blk-0", "pmsg");
  Put("/sys/fs/pstore/console-pstore_blk-0", "new console");
  Put(LAST_KMSG_FILE, "old console");
  assert(Main() == 0);
  assert(rotations == 1 && Get(LAST_KMSG_FILE) == "new console" &&
         Get(std::string(LAST_KMSG_FILE) + ".1") == "old console");
  assert(fs::exists(Map(BLK_LAST_CONSOLE_FILE)));
  ++cases;
  Reset();
  after_sleep = []() {
    if (millis >= 500) Put(BLK_LAST_PMSG_FILE, "late pmsg");
    if (millis >= 700) Put(BLK_LAST_CONSOLE_FILE, "late console");
  };
  assert(Main() == 0);
  assert(millis == 700 && pmsg_reads == 1 &&
         Get(LAST_KMSG_FILE) == "late console");
  ++cases;
  Reset();
  Put(BLK_LAST_CONSOLE_FILE, "console only");
  assert(Main() == 0);
  assert(millis == 30000 && clock_calls > 0 && sleeps == 300 &&
         pmsg_reads == 0 && Get(LAST_KMSG_FILE) == "console only");
  ++cases;
  Reset();
  assert(Main() == 0);
  assert(millis == 30000 && rotations == 0 && pmsg_reads == 0);
  ++cases;
  for (int error : {EACCES, EIO}) {
    Reset();
    Put(BLK_LAST_PMSG_FILE, "other");
    Put(BLK_LAST_CONSOLE_FILE, "other");
    fault_path = LAST_PMSG_FILE;
    fault_error = error;
    assert(Main() == 1);
    assert(errors > 0 && rotations == 0 && pmsg_reads == 0 && millis == 0);
    ++cases;
  }
  Reset();
  Put(BLK_LAST_PMSG_FILE, "pmsg");
  Put(BLK_LAST_CONSOLE_FILE, "console");
  Put(LAST_KMSG_FILE, "old");
  emit_pmsg = true;
  assert(Main() == 0);
  assert(rotations == 1 && Get(LAST_LOG_FILE) == "new recovery log" &&
         Get(LAST_KMSG_FILE) == "console");
  ++cases;
  Reset();
  Put(BLK_LAST_PMSG_FILE, "blk");
  Put(BLK_LAST_CONSOLE_FILE, "blk console");
  before_pmsg_open = []() { Put(LAST_PMSG_FILE, "new ramoops"); };
  assert(Main() == 0);
  assert(selected_pmsg == BLK_LAST_PMSG_FILE &&
         Get(LAST_KMSG_FILE) == "blk console");
  ++cases;
  Reset();
  Put(LAST_PMSG_FILE, "ramoops");
  Put(LAST_CONSOLE_FILE, "ramoops console");
  Put(BLK_LAST_PMSG_FILE, "other");
  before_pmsg_open = []() { fs::remove(Map(LAST_PMSG_FILE)); };
  assert(Main() == 1);
  assert(errors > 0 && rotations == 0 && selected_pmsg == LAST_PMSG_FILE &&
         !fs::exists(Map(LAST_KMSG_FILE)));
  ++cases;
  for (int error : {EACCES, EIO}) {
    Reset();
    Put(BLK_LAST_PMSG_FILE, "blk");
    Put(BLK_LAST_CONSOLE_FILE, "console");
    before_pmsg_open = [error]() {
      fault_path = BLK_LAST_PMSG_FILE;
      fault_error = error;
    };
    assert(Main() == 1);
    assert(errors > 0 && rotations == 0);
    ++cases;
  }
  std::cout << "Production collection/comparison tests passed: " << cases
            << " real-file cases; monotonic clock and sleep wrapped\n";
#endif
}
