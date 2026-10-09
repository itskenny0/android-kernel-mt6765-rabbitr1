// SPDX-License-Identifier: Apache-2.0
#pragma once
struct TestLog {
  template <class T>
  TestLog& operator<<(const T&) {
    return *this;
  }
  ~TestLog() { ++errors; }
};
#define LOG(level) TestLog()
#define PLOG(level) TestLog()
