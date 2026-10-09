#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""Decode the pinned GE8320 metadata, without executing or installing firmware."""

import argparse
import hashlib
import json
from pathlib import Path
import re
import struct
import sys


def checked_input(path, record, name):
    data = path.read_bytes()
    actual = hashlib.sha256(data).hexdigest()
    if actual != record["sha256"]:
        raise ValueError(f"{name}: SHA-256 mismatch: {actual}")
    if "bytes" in record and len(data) != record["bytes"]:
        raise ValueError(f"{name}: size mismatch")
    return data


def decode(data, abi, mapping, mesa_header):
    names = {}
    for kind in ("BRN", "ERN", "FEATURE"):
        entries = re.findall(r"PVR_FW_HAS_" + kind + r"_(\w+)", abi)
        if not entries or entries[-1] != "MAX":
            raise ValueError(f"{kind}: unsupported enum declaration")
        names[kind] = entries[:-1]

    value_map = dict(re.findall(
        r"^\s*FEATURE_MAPPING_VALUE\((\w+), (\w+)\),", mapping, re.M
    ))
    mesa_struct = mesa_header.split("struct pvr_device_features {", 1)[1]
    mesa_struct = mesa_struct.split("};", 1)[0]
    mesa_values = re.findall(r"uint32_t (\w+);", mesa_struct)
    mesa_flags = re.findall(r"bool has_(\w+)\s*:", mesa_struct)

    # Header layout from the pinned pvr_fw_info.h. This does not reproduce
    # pvr_fw_validate() or claim to validate the executable firmware image.
    footer = len(data) - 4096
    header = struct.unpack_from("<IIIIQIIHHIII", data, footer)
    if header[0] != 3 or header[1] != 48:
        raise ValueError("Unsupported firmware info version or header size")
    device_info_size = header[10]
    if not 32 <= device_info_size <= footer:
        raise ValueError("Invalid device-info size")
    offset = footer - device_info_size
    sizes = struct.unpack_from("<QQQQ", data, offset)
    if 32 + 8 * sum(sizes) > device_info_size:
        raise ValueError("Device-info arrays exceed the recorded size")
    offset += 32

    masks = {}
    for kind, count in zip(("BRN", "ERN", "FEATURE"), sizes[:3]):
        words = struct.unpack_from("<" + "Q" * count, data, offset)
        offset += 8 * count
        set_bits = [
            i for i in range(count * 64)
            if (words[i // 64] >> (i % 64)) & 1
        ]
        masks[kind] = {
            "words": [hex(word) for word in words],
            "known_set": {
                str(i): names[kind][i] for i in set_bits if i < len(names[kind])
            },
            "unsupported_indices": [i for i in set_bits if i >= len(names[kind])],
        }

    params = struct.unpack_from("<" + "Q" * sizes[3], data, offset)
    values = {}
    consumed = 0
    present = list(masks["FEATURE"]["known_set"].values())
    for name in present:
        if name in value_map:
            if consumed >= len(params):
                raise ValueError("Known numeric feature exceeds parameter count")
            values[value_map[name]] = params[consumed]
            consumed += 1

    bvnc = ".".join(str((header[4] >> shift) & 0xffff)
                    for shift in (48, 32, 16, 0))
    return {
        "firmware_sha256": hashlib.sha256(data).hexdigest(),
        "firmware_version": f"{header[7]}.{header[8]}.OS@{header[9]}",
        "bvnc": bvnc,
        "masks": masks,
        "feature_parameters": list(params),
        "numeric_coverage": {
            "decoded_known_values": values,
            "parameters_present": len(params),
            "known_parameters_consumed": consumed,
            "unused_parameter_tail": consumed < len(params),
            "mesa_numeric_fields_not_defined_in_published_abi": [
                name for name in mesa_values if name.upper() not in names["FEATURE"]
            ],
            "other_mesa_numeric_fields_defined_but_unset": [
                name for name in mesa_values
                if name.upper() in names["FEATURE"] and name not in values
            ],
        },
        "mesa_same_name_flags_set_by_firmware": [
            name for name in mesa_flags if name.upper() in present
        ],
        "validation_scope": (
            "Pinned metadata decoded only. Unknown indices remain unresolved; "
            "this does not validate firmware execution or hardware support."
        ),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--firmware", required=True, type=Path)
    parser.add_argument("--firmware-id", choices=("current", "previous"),
                        default="current")
    parser.add_argument("--kernel-abi", required=True, type=Path)
    parser.add_argument("--kernel-mapping", required=True, type=Path)
    parser.add_argument("--mesa-header", required=True, type=Path)
    args = parser.parse_args()

    try:
        provenance = json.loads(Path(__file__).with_name("provenance.json").read_text())
        fw_record = provenance["firmwares"][args.firmware_id]
        firmware = checked_input(args.firmware, fw_record, "firmware")
        sources = {
            name: checked_input(getattr(args, name), record, name).decode("utf-8")
            for name, record in provenance["inputs"].items()
        }
        result = decode(firmware, sources["kernel_abi"], sources["kernel_mapping"],
                        sources["mesa_header"])
        if result["bvnc"] != provenance["target"]["bvnc"]:
            raise ValueError("Firmware BVNC differs from the audit target")
        if result["firmware_version"] != fw_record["version"]:
            raise ValueError("Firmware version differs from the audit record")
    except (OSError, ValueError, KeyError, IndexError, struct.error) as error:
        parser.exit(1, f"Metadata audit failed: {error}\n")

    json.dump(result, sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
