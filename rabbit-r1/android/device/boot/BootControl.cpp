// SPDX-License-Identifier: Apache-2.0
#include "BootControl.h"

#include <android-base/logging.h>
#include <libboot_control/libboot_control.h>

using ndk::ScopedAStatus;
using HIDLMergeStatus = ::android::hardware::boot::V1_1::MergeStatus;

namespace aidl::android::hardware::boot {
namespace {
ScopedAStatus Status(const haretic::boot::Result& result) {
    if (result) return ScopedAStatus::ok();
    LOG(ERROR) << result.error;
    return ScopedAStatus::fromServiceSpecificErrorWithMessage(IBootControl::COMMAND_FAILED,
                                                            result.error.c_str());
}
ScopedAStatus InvalidSlot() {
    return ScopedAStatus::fromServiceSpecificErrorWithMessage(IBootControl::INVALID_SLOT,
                                                            "Invalid r1 slot");
}
}  // namespace

BootControl::BootControl(unsigned current, const std::string& misc)
    : current_(current), control_(storage_, current) {
    CHECK(storage_.Open(misc)) << storage_.Error();
    haretic::boot::Record record;
    const auto result = control_.Load(&record);
    CHECK(result.ok) << result.error;
    // Keep the standard separate merge-status record and semantics. Do not
    // call AOSP BootControl::Init(), which would rewrite an invalid A/B record.
    CHECK(::android::bootable::InitMiscVirtualAbMessageIfNeeded());
}

ScopedAStatus BootControl::getNumberSlots(int32_t* value) {
    *value = 2;
    return ScopedAStatus::ok();
}

ScopedAStatus BootControl::getCurrentSlot(int32_t* value) {
    *value = current_;
    return ScopedAStatus::ok();
}

ScopedAStatus BootControl::getSuffix(int32_t slot, std::string* value) {
    *value = slot == 0 ? "_a" : slot == 1 ? "_b" : "";
    return ScopedAStatus::ok();
}

ScopedAStatus BootControl::getActiveBootSlot(int32_t* value) {
    std::lock_guard lock(mutex_);
    unsigned slot = current_;
    const auto result = control_.Active(&slot);
    if (result) *value = slot;
    return Status(result);
}

ScopedAStatus BootControl::isSlotBootable(int32_t slot, bool* value) {
    if (!haretic::boot::Control::ValidSlot(slot)) return InvalidSlot();
    std::lock_guard lock(mutex_);
    return Status(control_.Bootable(slot, value));
}

ScopedAStatus BootControl::isSlotMarkedSuccessful(int32_t slot, bool* value) {
    if (!haretic::boot::Control::ValidSlot(slot)) return InvalidSlot();
    std::lock_guard lock(mutex_);
    return Status(control_.Successful(slot, value));
}

ScopedAStatus BootControl::markBootSuccessful() {
    std::lock_guard lock(mutex_);
    return Status(control_.MarkSuccessful());
}

ScopedAStatus BootControl::setActiveBootSlot(int32_t slot) {
    if (!haretic::boot::Control::ValidSlot(slot)) return InvalidSlot();
    std::lock_guard lock(mutex_);
    return Status(control_.SetActive(slot));
}

ScopedAStatus BootControl::setSlotAsUnbootable(int32_t slot) {
    if (!haretic::boot::Control::ValidSlot(slot)) return InvalidSlot();
    std::lock_guard lock(mutex_);
    return Status(control_.SetUnbootable(slot));
}

ScopedAStatus BootControl::getSnapshotMergeStatus(MergeStatus* value) {
    std::lock_guard lock(mutex_);
    HIDLMergeStatus status;
    if (!::android::bootable::GetMiscVirtualAbMergeStatus(current_, &status)) {
        return Status({false, "Cannot read snapshot merge status"});
    }
    switch (status) {
        case HIDLMergeStatus::NONE: *value = MergeStatus::NONE; break;
        case HIDLMergeStatus::UNKNOWN: *value = MergeStatus::UNKNOWN; break;
        case HIDLMergeStatus::SNAPSHOTTED: *value = MergeStatus::SNAPSHOTTED; break;
        case HIDLMergeStatus::MERGING: *value = MergeStatus::MERGING; break;
        case HIDLMergeStatus::CANCELLED: *value = MergeStatus::CANCELLED; break;
        default: return Status({false, "Invalid snapshot merge status"});
    }
    return ScopedAStatus::ok();
}

ScopedAStatus BootControl::setSnapshotMergeStatus(MergeStatus value) {
    std::lock_guard lock(mutex_);
    HIDLMergeStatus status;
    switch (value) {
        case MergeStatus::NONE: status = HIDLMergeStatus::NONE; break;
        case MergeStatus::UNKNOWN: status = HIDLMergeStatus::UNKNOWN; break;
        case MergeStatus::SNAPSHOTTED: status = HIDLMergeStatus::SNAPSHOTTED; break;
        case MergeStatus::MERGING: status = HIDLMergeStatus::MERGING; break;
        case MergeStatus::CANCELLED: status = HIDLMergeStatus::CANCELLED; break;
        default: return ScopedAStatus::fromExceptionCode(EX_ILLEGAL_ARGUMENT);
    }
    if (!::android::bootable::SetMiscVirtualAbMergeStatus(current_, status)) {
        return Status({false, "Cannot save snapshot merge status"});
    }
    return ScopedAStatus::ok();
}

}  // namespace aidl::android::hardware::boot
