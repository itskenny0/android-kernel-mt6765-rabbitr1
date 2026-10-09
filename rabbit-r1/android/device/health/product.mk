# SPDX-License-Identifier: Apache-2.0
# Evaluate before mainline_common.mk selects its default Health package.
include device/rabbit/r1/prebuilt/health-prebuilt.mk
ifneq ($(R1_INSTALLED_HEALTH_PRODUCT),$(TARGET_PRODUCT))
$(error Reinstall r1 device inputs for the selected Android product)
endif
ifeq ($(TARGET_PRODUCT),lineage_r1_soc)
ifneq ($(R1_PREBUILT_SOC_CONFIG),y)
$(error Experimental Health requires CONFIG_BATTERY_MT6357_R1_SOC=y in the verified prebuilt)
endif
TARGET_HEALTH_HAL := r1
PRODUCT_PACKAGES += \
    android.hardware.health-service.r1 \
    r1-health-initial-ready \
    android.hardware.health-service.example_recovery \
    charger_res_images_vendor
else ifeq ($(TARGET_PRODUCT),lineage_r1)
ifneq ($(R1_PREBUILT_SOC_CONFIG),n)
$(error The shipping product requires CONFIG_BATTERY_MT6357_R1_SOC disabled)
endif
TARGET_HEALTH_HAL := default-aidl
else
$(error Unsupported r1 product)
endif
