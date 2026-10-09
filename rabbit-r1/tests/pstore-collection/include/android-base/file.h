// SPDX-License-Identifier: Apache-2.0
#pragma once
namespace android::base {
inline bool ReadFully(int fd, void* data, size_t count) {
  auto* p = static_cast<char*>(data);
  while (count) {
    ssize_t n = ::read(fd, p, count);
    if (n < 0 && errno == EINTR) continue;
    if (n <= 0) return false;
    p += n;
    count -= n;
  }
  return true;
}
inline bool ReadFileToString(const std::string& path, std::string* out) {
  std::ifstream f(Map(path));
  if (!f) return false;
  *out = std::string(std::istreambuf_iterator<char>(f), {});
  return !f.bad();
}
inline bool WriteStringToFile(const std::string& data, const char* path) {
  std::ofstream f(Map(path));
  f << data;
  return bool(f);
}
}  // namespace android::base
