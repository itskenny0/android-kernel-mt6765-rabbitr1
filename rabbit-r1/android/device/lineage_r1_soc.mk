# SPDX-License-Identifier: Apache-2.0
# Explicit experimental product; the installer verifies its kernel SOC config.
$(call inherit-product, device/rabbit/r1/lineage_r1.mk)
PRODUCT_NAME := lineage_r1_soc
PRODUCT_MODEL := r1 (experimental SOC)
