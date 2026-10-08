// SPDX-License-Identifier: Apache-2.0
#pragma once

#include <array>
#include <cstdint>
#include <string>

namespace haretic::boot {

using Record = std::array<uint8_t, 32>;

// Record writes must be synchronous and verified by readback, confined to the
// 32-byte record at offset 2048 in para. Control verifies selector readback.
class Storage {
  public:
    virtual ~Storage() = default;
    virtual bool ReadRecord(Record* record) = 0;
    virtual bool WriteRecord(const Record& record) = 0;
    virtual bool ReadBootConfig(uint8_t* config) = 0;
    virtual bool WriteBootConfig(uint8_t config) = 0;
    virtual std::string Error() const = 0;
};

struct Result {
    bool ok;
    std::string error;
    explicit operator bool() const { return ok; }
};

// One service owns this object. Its Binder wrapper serializes all operations.
class Control {
  public:
    Control(Storage& storage, unsigned current) : storage_(storage), current_(current) {}
    static uint32_t Crc(const Record& record);
    static void Seal(Record* record);
    static bool Valid(const Record& record);
    static bool ValidSlot(int32_t slot) { return slot >= 0 && slot < 2; }
    Result Load(Record* record);
    Result Active(unsigned* slot);
    Result Bootable(unsigned slot, bool* value);
    Result Successful(unsigned slot, bool* value);
    Result MarkSuccessful();
    Result SetUnbootable(unsigned slot);
    Result SetActive(unsigned slot);

  private:
    Result Save(const Record& before, Record next);
    std::string IoError(const char* operation) const;
    Storage& storage_;
    unsigned current_;
};

}  // namespace haretic::boot
