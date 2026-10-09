// SPDX-License-Identifier: Apache-2.0
#pragma once
using log_id_t = int;
constexpr int LOG_ID_SYSTEM = 1, ANDROID_LOG_INFO = 4;
using PmsgCallback = ssize_t (*)(log_id_t, char, const char*, const char*,
                                 size_t, void*);
ssize_t __android_log_pmsg_file_read(log_id_t, char, const char*, PmsgCallback,
                                     void*);
ssize_t __android_log_pmsg_file_read_from_backend(const char*, log_id_t, char,
                                                  const char*, PmsgCallback,
                                                  void*);
