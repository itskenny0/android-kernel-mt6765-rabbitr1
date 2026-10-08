// SPDX-License-Identifier: Apache-2.0
#pragma once
#include "Control.h"

namespace haretic::boot {

class LinuxStorage final : public Storage {
  public:
    LinuxStorage() = default;
    ~LinuxStorage() override;
    LinuxStorage(const LinuxStorage&) = delete;
    LinuxStorage& operator=(const LinuxStorage&) = delete;
    bool Open(const std::string& misc_path);
    bool ReadRecord(Record* record) override;
    bool WriteRecord(const Record& record) override;
    bool ReadBootConfig(uint8_t* config) override;
    bool WriteBootConfig(uint8_t config) override;
    std::string Error() const override { return error_; }

  private:
    bool Fail(const char* operation);
    bool Reject(const char* message);
    int misc_ = -1;
    int misc_read_ = -1;
    int emmc_ = -1;
    std::string error_;
};

}  // namespace haretic::boot
