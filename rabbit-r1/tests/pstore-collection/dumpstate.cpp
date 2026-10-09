// SPDX-License-Identifier: Apache-2.0
#include <sys/stat.h>

#include <cassert>
#include <cerrno>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
namespace fs = std::filesystem;
static const std::string root = R1_TEST_DIRECTORY;
static std::string dumped, fault;
static int fault_errno;
int TestStat(const char* p, struct stat* s) {
  assert(std::string(p).starts_with("/sys/fs/pstore/"));
  if (p == fault) {
    errno = fault_errno;
    return -1;
  }
  return ::stat((root + "/" + fs::path(p).filename().string()).c_str(), s);
}
void DumpFile(const char* title, const char* path) {
  assert(std::string(title) == "LAST KMSG");
  dumped = path;
}
#define stat(...) TestStat(__VA_ARGS__)
#include R1_DUMPSTATE_FUNCTION
#undef stat
void Reset() {
  fs::remove_all(root);
  fs::create_directories(root);
  dumped.clear();
  fault.clear();
  fault_errno = 0;
}
void Put(const std::string& p) {
  std::ofstream f(root + "/" + fs::path(p).filename().string());
  f << "console";
  assert(f.good());
}
int main() {
  [[maybe_unused]] int cases = 0;
  Reset();
  Put("/sys/fs/pstore/console-pstore_blk-0");
  DoKmsg();
#ifdef ORIGINAL
  assert(dumped == "/proc/last_kmsg");
  std::cout << "PASS: original DoKmsg ignores the present pstore_blk console\n";
#else
  assert(dumped == BLK_PSTORE_LAST_KMSG);
  ++cases;
  Put(ALT_PSTORE_LAST_KMSG);
  DoKmsg();
  assert(dumped == ALT_PSTORE_LAST_KMSG);
  ++cases;
  Put(PSTORE_LAST_KMSG);
  DoKmsg();
  assert(dumped == PSTORE_LAST_KMSG);
  ++cases;
  Reset();
  DoKmsg();
  assert(dumped == "/proc/last_kmsg");
  ++cases;
  for (auto p : {PSTORE_LAST_KMSG, ALT_PSTORE_LAST_KMSG, BLK_PSTORE_LAST_KMSG})
    for (int error : {EACCES, EIO}) {
      Reset();
      fault = p;
      fault_errno = error;
      DoKmsg();
      assert(dumped == p);
      ++cases;
    }
  std::cout << "PASS: " << cases
            << " production DoKmsg cases: backend preference and error "
               "reporting preserved\n";
#endif
}
