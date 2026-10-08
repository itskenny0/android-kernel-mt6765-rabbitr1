# SPDX-License-Identifier: Apache-2.0
$(call inherit-product, $(SRC_TARGET_DIR)/product/core_64_bit_only.mk)
$(call inherit-product, $(SRC_TARGET_DIR)/product/full_base.mk)
$(call inherit-product, device/rabbit/r1/device.mk)
$(call inherit-product, vendor/lineage/config/common_full_tablet_wifionly.mk)

PRODUCT_NAME := lineage_r1
PRODUCT_DEVICE := r1
PRODUCT_BRAND := Rabbit
PRODUCT_MANUFACTURER := Rabbit
PRODUCT_MODEL := r1
PRODUCT_CHARACTERISTICS := tablet

# Effective value in the stock boot ramdisk's final sysprop.txt section.
PRODUCT_SHIPPING_API_LEVEL := 33
