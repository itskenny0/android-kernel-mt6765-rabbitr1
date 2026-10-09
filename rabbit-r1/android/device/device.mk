# SPDX-License-Identifier: Apache-2.0
LOCAL_PATH := device/rabbit/r1

# Keep these explicit: the common bring-up defaults include a simulated battery.
TARGET_INITIAL_BRINGUP := true
TARGET_HAS_BATTERY := true
TARGET_HEALTH_HAL := default-aidl
TARGET_GRAPHICS := swiftshader
TARGET_GRAPHICS_COMPOSER_HAL := drmfb-composer
TARGET_SUPPORTS_SUSPEND := false
TARGET_BOOT_HAL := r1
TARGET_FOLLOWS_LATEST_VINTF_TARGET_LEVEL := true

TARGET_PREBUILT_KERNEL := device/rabbit/r1/prebuilt/Image.gz
TARGET_KERNEL_MIXED_MODE := false
TARGET_FORCE_PREBUILT_KERNEL := true

# Dedicated A/B extents use the upstream dm-linear driver. Stock uses virtual
# A/B; do not inherit its userspace snapshots without a working dm-user driver.
AB_OTA_UPDATER := true
AB_OTA_PARTITIONS += boot dtbo system system_ext product vendor vbmeta
PRODUCT_USE_DYNAMIC_PARTITIONS := true
PRODUCT_BUILD_SUPER_PARTITION := true
PRODUCT_VIRTUAL_AB_OTA := false
PRODUCT_VIRTUAL_AB_COMPRESSION := false

$(call inherit-product, device/mainline/common/mainline_common.mk)
$(call inherit-product, device/rabbit/r1/charging/product.mk)

PRODUCT_PACKAGES += \
    android.hardware.boot-service.r1 \
    android.hardware.boot-service.r1_recovery \
    r1-expdb

# The logger validates expdb and bounds its mapping before loading pstore.
PRODUCT_VENDOR_PROPERTIES += ro.vendor.r1.expdb.enabled=1

# Recovery-as-boot copies root/ into its ramdisk for the normal-boot switch.
PRODUCT_COPY_FILES += \
    $(LOCAL_PATH)/rootdir/fstab.r1:$(TARGET_COPY_OUT_VENDOR)/etc/fstab.r1 \
    $(LOCAL_PATH)/rootdir/fstab.r1:$(TARGET_COPY_OUT_ROOT)/first_stage_ramdisk/fstab.r1 \
    $(LOCAL_PATH)/rootdir/init.r1.rc:$(TARGET_COPY_OUT_VENDOR)/etc/init/init.r1.rc

# Match the physical panel. Revisit density after UI testing on the device.
TARGET_SCREEN_WIDTH := 480
TARGET_SCREEN_HEIGHT := 640
PRODUCT_AAPT_CONFIG := normal
PRODUCT_AAPT_PREF_CONFIG := hdpi
PRODUCT_VENDOR_PROPERTIES += ro.sf.lcd_density=240
