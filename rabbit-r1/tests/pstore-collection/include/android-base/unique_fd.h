// SPDX-License-Identifier: Apache-2.0
#pragma once
namespace android::base {
class unique_fd {
  int fd_;

 public:
  explicit unique_fd(int fd) : fd_(fd) {}
  ~unique_fd() {
    if (fd_ >= 0) ::close(fd_);
  }
  operator int() const { return fd_; }
};
}  // namespace android::base
