#
# Copyright (C) 2007 The Android Open Source Project
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Exact inherit-product macro excerpt; see sources.json.
define inherit-product
  $(eval _inherit_product_wildcard := $(wildcard $(1)))\
  $(if $(_inherit_product_wildcard),,$(error $(1) does not exist.))\
  $(foreach part,$(_inherit_product_wildcard),\
    $(if $(findstring ../,$(part)),\
      $(eval np := $(call normalize-paths,$(part))),\
      $(eval np := $(strip $(part))))\
    $(foreach v,$(_product_var_list), \
        $(eval $(v) := $($(v)) $(INHERIT_TAG)$(np))) \
    $(eval current_mk := $(strip $(word 1,$(_include_stack)))) \
    $(eval inherit_var := PRODUCTS.$(current_mk).INHERITS_FROM) \
    $(eval $(inherit_var) := $(sort $($(inherit_var)) $(np))) \
    $(call dump-inherit,$(current_mk),$(1)) \
    $(call dump-config-vals,$(current_mk),inherit))
endef
