// SPDX-License-Identifier: Apache-2.0
#pragma once
#include <cstddef>
#include <string>
#include <sys/types.h>

namespace haretic::logging {
// This entry point accepts no paths, geometry, parameters or module names.
// A false result never authorizes use of the raw expdb partition for logging.
bool StartExpdb(std::string* error);

// Checks the actual DM_TABLE_STATUS response, including bounded string parsing.
bool ValidateExpdbTable(const void* buffer, size_t capacity, dev_t backing,
                        dev_t mapper, const std::string& uuid, std::string* error);
}  // namespace haretic::logging
