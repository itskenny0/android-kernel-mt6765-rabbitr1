// SPDX-License-Identifier: Apache-2.0
#include <fcntl.h>
#include <private/android_logger.h>
#include <unistd.h>

#include <cassert>
#include <cerrno>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <map>
#include <set>
#include <string>
#include <vector>

#include "logger.h"
#include "pmsg_reader.h"
// Include the production body to inspect its existing pid/time filter paths,
// which the file-reader API does not expose. Other regressions link it
// separately.
#include R1_PMSG_READER_SOURCE
namespace fs = std::filesystem;
static const std::string root = R1_TEST_DIRECTORY;
static const char* ram = "/sys/fs/pstore/pmsg-ramoops-0";
static const char* blk = "/sys/fs/pstore/pmsg-pstore_blk-0";
static std::set<int> fds;
static std::vector<std::string> opens, contents;
static std::string fail_open;
static int open_error, read_error, fail_read_at, reads, seeks, cases;
extern "C" int __real_open(const char*, int, ...);
extern "C" int __real_close(int);
extern "C" ssize_t __real_read(int, void*, size_t);
extern "C" off_t __real_lseek(int, off_t, int);
extern "C" int __wrap_open(const char* path, int flags, ...) {
  assert(std::string(path) == ram || std::string(path) == blk);
  assert(flags == (O_RDONLY | O_CLOEXEC));
  opens.emplace_back(path);
  if (path == fail_open) {
    errno = open_error;
    return -1;
  }
  auto name = root + (std::string(path) == ram ? "/ram" : "/blk");
  int fd = __real_open(name.c_str(), flags);
  if (fd >= 0) assert(fds.insert(fd).second);
  return fd;
}
extern "C" int __wrap_close(int fd) {
  assert(fds.erase(fd) == 1);
  return __real_close(fd);
}
extern "C" ssize_t __wrap_read(int fd, void* p, size_t count) {
  assert(fds.contains(fd));
  ++reads;
  if (read_error && (!fail_read_at || reads == fail_read_at)) {
    errno = read_error;
    return -1;
  }
  return __real_read(fd, p, count);
}
extern "C" off_t __wrap_lseek(int fd, off_t offset, int whence) {
  assert(fds.contains(fd));
  ++seeks;
  return __real_lseek(fd, offset, whence);
}
void Put(const char* name, const std::string& data) {
  std::ofstream f(root + name, std::ios::binary);
  f.write(data.data(), data.size());
  assert(f.good());
}
void Reset() {
  assert(fds.empty());
  fs::remove_all(root);
  fs::create_directories(root);
  opens.clear();
  contents.clear();
  fail_open.clear();
  open_error = read_error = fail_read_at = reads = seeks = 0;
  ++cases;
}
std::string Packet(const std::string& content) {
  const std::string tag = "recovery:last_log";
  std::string payload(1, ANDROID_LOG_INFO);
  payload += tag;
  payload += '\0';
  payload += content;
  android_pmsg_log_header_t p{};
  p.magic = LOGGER_MAGIC;
  p.len = sizeof(p) + sizeof(android_log_header_t) + payload.size();
  p.uid = 1000;
  p.pid = 123;
  android_log_header_t l{};
  l.id = LOG_ID_SYSTEM;
  l.tid = 123;
  l.realtime.tv_sec = 10;
  l.realtime.tv_nsec = 0;
  return std::string(reinterpret_cast<const char*>(&p), sizeof(p)) +
         std::string(reinterpret_cast<const char*>(&l), sizeof(l)) + payload;
}
ssize_t Receive(log_id_t id, char prio, const char* file, const char* buf,
                size_t len, void*) {
  assert(id == LOG_ID_SYSTEM && prio == ANDROID_LOG_INFO &&
         std::string(file) == "recovery/last_log");
  contents.emplace_back(buf, len);
  return len;
}
ssize_t Read(const char* backend) {
  return __android_log_pmsg_file_read_from_backend(
      backend, LOG_ID_SYSTEM, ANDROID_LOG_INFO, "recovery/", Receive, nullptr);
}
int main() {
  Reset();
  Put("/ram", Packet("ramoops"));
  Put("/blk", Packet("blk"));
  assert(Read("pstore_blk") == 3);
  assert(contents == std::vector<std::string>{"blk"} &&
         opens == std::vector<std::string>{blk});
  Reset();
  Put("/ram", Packet("ramoops"));
  Put("/blk", Packet("blk"));
  assert(Read("ramoops") == 7);
  assert(contents == std::vector<std::string>{"ramoops"} &&
         opens == std::vector<std::string>{ram});
  for (int error : {ENOENT, EACCES, EIO}) {
    Reset();
    Put("/blk", Packet("other"));
    fail_open = ram;
    open_error = error;
    assert(Read("ramoops") == -error);
    assert(contents.empty() && opens == std::vector<std::string>{ram});
  }
  Reset();
  Put("/ram", Packet("other"));
  assert(Read("pstore_blk") == -ENOENT);
  assert(contents.empty() && opens == std::vector<std::string>{blk});
  Reset();
  assert(Read(nullptr) == -EINVAL && Read("../ramoops") == -EINVAL &&
         Read("auto") == -EINVAL && opens.empty());
  Reset();
  Put("/blk", "");
  assert(Read("pstore_blk") == 0 && contents.empty());
  Reset();
  Put("/blk", Packet("valid") + "bad");
  assert(Read("pstore_blk") == -EIO && contents.empty());
  for (int error : {EIO, EAGAIN})
    for (int at : {1, 3}) {
      Reset();
      Put("/blk", Packet("valid"));
      read_error = error;
      fail_read_at = at;
      assert(Read("pstore_blk") == -error && contents.empty());
    }
  Reset();
  Put("/ram", Packet("previous").substr(0, 4));
  logger_list list{};
  list.log_mask = ~0U;
  log_msg msg{};
  assert(PmsgRead(&list, &msg) == -EIO);
  PmsgClose(&list);
  Put("/blk", Packet("after partial"));
  assert(Read("pstore_blk") == 13 &&
         contents == std::vector<std::string>{"after partial"});
  assert(opens == std::vector<std::string>({ram, blk}));
  // A valid fragment must not hide a truncated record excluded by log id.
  auto filtered = Packet("filtered payload");
  filtered[sizeof(android_pmsg_log_header_t)] = LOG_ID_MAIN;
  Reset();
  Put("/blk", Packet("valid") + filtered);
  assert(Read("pstore_blk") == 5 &&
         contents == std::vector<std::string>{"valid"} && seeks == 0);
  auto truncated = filtered.substr(
      0, sizeof(android_pmsg_log_header_t) + sizeof(android_log_header_t) + 1);
  Reset();
  Put("/blk", Packet("valid") + truncated);
  assert(Read("pstore_blk") == -EIO && contents.empty() && seeks == 0);
  for (int filter : {0, 1, 2})
    for (bool cut : {false, true}) {
      Reset();
      auto packet = Packet("skipped");
      if (cut)
        packet.resize(sizeof(android_pmsg_log_header_t) +
                      sizeof(android_log_header_t) + 1);
      Put("/blk", packet);
      logger_list filtered_list{};
      filtered_list.log_mask = ~0U;
      if (filter == 0) filtered_list.log_mask = 1U << LOG_ID_MAIN;
      if (filter == 1) filtered_list.pid = 999;
      if (filter == 2) filtered_list.start.tv_sec = 20;
      log_msg filtered_msg{};
      assert(PmsgReadFromPath(&filtered_list, &filtered_msg, blk) ==
             (cut ? -EIO : 0));
      PmsgClose(&filtered_list);
      assert(seeks == 0);
    }
  // Keep the legacy skipped-record behavior isolated to automatic callers.
  Reset();
  Put("/ram", Packet("valid") + truncated);
  assert(__android_log_pmsg_file_read(LOG_ID_SYSTEM, ANDROID_LOG_INFO,
                                      "recovery/", Receive, nullptr) == 5 &&
         contents == std::vector<std::string>{"valid"} && seeks > 0);
  // Preserve the old ABI, automatic fallback, and no-match return convention.
  Reset();
  Put("/blk", Packet("automatic"));
  assert(__android_log_pmsg_file_read(LOG_ID_SYSTEM, ANDROID_LOG_INFO,
                                      "recovery/", Receive, nullptr) == 9);
  assert(opens == std::vector<std::string>({ram, blk}));
  Reset();
  Put("/ram", "");
  assert(__android_log_pmsg_file_read(LOG_ID_SYSTEM, ANDROID_LOG_INFO,
                                      "recovery/", Receive,
                                      nullptr) == -ENOENT);
  Reset();
  Put("/ram", Packet("no fallback"));
  fail_open = ram;
  open_error = EACCES;
  assert(__android_log_pmsg_file_read(LOG_ID_SYSTEM, ANDROID_LOG_INFO,
                                      "recovery/", Receive,
                                      nullptr) == -ENOENT &&
         opens == std::vector<std::string>{ram});
  assert(fds.empty());
  std::cout << "PASS: " << cases
            << " actual liblog reader cases with real temp files; selected "
               "backend, errors, ABI and preread reset\n";
}
