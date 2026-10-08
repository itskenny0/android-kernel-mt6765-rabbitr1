// SPDX-License-Identifier: Apache-2.0
#include "Control.h"

namespace haretic::boot {
namespace {
constexpr unsigned kSlots = 2;
constexpr unsigned kSlotStart = 12;
constexpr uint8_t kBootMask = 0x38;
Result Ok() { return {true, {}}; }
Result Fail(const std::string& error) { return {false, error}; }
}  // namespace

uint32_t Control::Crc(const Record& record) {
    uint32_t crc = ~uint32_t{0};
    for (unsigned i = 0; i < 28; ++i) {
        crc ^= record[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            crc = (crc >> 1) ^ (0xedb88320U & (0U - (crc & 1U)));
        }
    }
    return ~crc;
}

void Control::Seal(Record* record) {
    const uint32_t crc = Crc(*record);
    for (unsigned i = 0; i < 4; ++i) (*record)[28 + i] = crc >> (8 * i);
}

bool Control::Valid(const Record& record) {
    if (record[4] != 'B' || record[5] != 'C' || record[6] != 'A' || record[7] != 'B' ||
        record[8] != 1 || (record[9] & 7) != kSlots) return false;
    const uint32_t crc = Crc(record);
    for (unsigned i = 0; i < 4; ++i) {
        if (record[28 + i] != ((crc >> (8 * i)) & 255)) return false;
    }
    return true;
}

std::string Control::IoError(const char* operation) const {
    const std::string detail = storage_.Error();
    return std::string(operation) + (detail.empty() ? "" : ": " + detail);
}

Result Control::Load(Record* record) {
    if (current_ >= kSlots) return Fail("Invalid current slot");
    if (!storage_.ReadRecord(record)) return Fail(IoError("Cannot read boot record"));
    if (!Valid(*record)) return Fail("Invalid boot record; refusing to overwrite it");
    return Ok();
}

Result Control::Active(unsigned* slot) {
    Record record;
    const Result result = Load(&record);
    if (!result) return result;
    *slot = current_;
    for (unsigned i = 0; i < kSlots; ++i) {
        if ((record[kSlotStart + 2 * i] & 15) > (record[kSlotStart + 2 * *slot] & 15)) {
            *slot = i;
        }
    }
    return Ok();
}

Result Control::Bootable(unsigned slot, bool* value) {
    if (slot >= kSlots) return Fail("Invalid slot");
    Record record;
    const Result result = Load(&record);
    if (!result) return result;
    *value = (record[kSlotStart + 2 * slot] & 0x70) != 0;
    return Ok();
}

Result Control::Successful(unsigned slot, bool* value) {
    if (slot >= kSlots) return Fail("Invalid slot");
    Record record;
    const Result result = Load(&record);
    if (!result) return result;
    const uint8_t entry = record[kSlotStart + 2 * slot];
    *value = (entry & 0x80) && (entry & 0x70);
    return Ok();
}

Result Control::Save(const Record& before, Record next) {
    Seal(&next);
    if (next == before || storage_.WriteRecord(next)) return Ok();
    const std::string error = IoError("Cannot save boot record");
    if (!storage_.WriteRecord(before)) {
        return Fail(error + "; " + IoError("record restoration also failed"));
    }
    return Fail(error + "; original record restored");
}

Result Control::MarkSuccessful() {
    Record before;
    const Result result = Load(&before);
    if (!result) return result;
    Record next = before;
    next[kSlotStart + 2 * current_] = (next[kSlotStart + 2 * current_] & 15) | 0x90;
    // Stock clears avbbctl after marking success. Combine both mutations in
    // one checked record write, preserving every other reserved byte.
    if (next[20] == 1) next[20] = 0;
    return Save(before, next);
}

Result Control::SetUnbootable(unsigned slot) {
    if (slot >= kSlots) return Fail("Invalid slot");
    Record before;
    const Result result = Load(&before);
    if (!result) return result;
    Record next = before;
    next[kSlotStart + 2 * slot] &= 15;
    return Save(before, next);
}

Result Control::SetActive(unsigned slot) {
    if (slot >= kSlots) return Fail("Invalid slot");
    Record before;
    Result result = Load(&before);
    if (!result) return result;
    uint8_t old_config;
    if (!storage_.ReadBootConfig(&old_config)) return Fail(IoError("Cannot read eMMC boot region"));
    const unsigned old_region = (old_config & kBootMask) >> 3;
    if (old_region != 1 && old_region != 2) {
        return Fail("eMMC is not configured for either r1 boot region");
    }

    Record next = before;
    for (unsigned i = 0; i < kSlots; ++i) {
        uint8_t& entry = next[kSlotStart + 2 * i];
        if (i != slot && (entry & 15) == 15) entry = (entry & 0xf0) | 14;
    }
    next[kSlotStart + 2 * slot] = (next[kSlotStart + 2 * slot] & 0x80) | 0x6f;
    if (slot != current_) next[kSlotStart + 2 * slot + 1] &= ~uint8_t{1};
    result = Save(before, next);
    if (!result) return result;  // The hardware selector has not been touched.

    const uint8_t target = (old_config & ~kBootMask) | ((slot + 1) << 3);
    if (target == old_config) return Ok();
    uint8_t readback;
    if (storage_.WriteBootConfig(target) && storage_.ReadBootConfig(&readback) &&
        readback == target) return Ok();

    const std::string error = IoError("eMMC boot-region change could not be verified");
    // Even an ioctl error may mean the card accepted the switch. Force a
    // restoring command so the kernel's cached PARTITION_CONFIG is refreshed.
    // Restore metadata only once the old hardware selection is verified.
    if (!storage_.WriteBootConfig(old_config) || !storage_.ReadBootConfig(&readback) ||
        readback != old_config) {
        return Fail(error + "; eMMC restoration failed; new metadata retained, recovery required");
    }
    if (!storage_.WriteRecord(before)) {
        return Fail(error + "; old eMMC region restored but metadata restoration failed");
    }
    return Fail(error + "; original region and record restored");
}

}  // namespace haretic::boot
