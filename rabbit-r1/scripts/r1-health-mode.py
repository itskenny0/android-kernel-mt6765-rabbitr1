#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Validate the explicit Health product against the actual prebuilt kernel."""
import hashlib
import re
import zlib

PRODUCTS = ('lineage_r1', 'lineage_r1_soc')
SOC_CONFIG = 'CONFIG_BATTERY_MT6357_R1_SOC'
MAX_CONFIG_BYTES = 1024 * 1024
MAX_IMAGE_BYTES = 256 * 1024 * 1024


def digest(data):
    return hashlib.sha256(data).hexdigest()


def gzip_member(data, limit):
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    try:
        decoded = decoder.decompress(data, limit + 1)
    except zlib.error as error:
        raise ValueError('Invalid embedded gzip data') from error
    if len(decoded) > limit or not decoder.eof or decoder.unconsumed_tail:
        raise ValueError('Truncated or oversized gzip data')
    return decoded, decoder.unused_data


def config_options(config):
    if len(config) > MAX_CONFIG_BYTES:
        raise ValueError('Kernel config is too large')
    try:
        text = config.decode('ascii')
    except UnicodeDecodeError as error:
        raise ValueError('Kernel config must be ASCII') from error
    options = {}
    for line in text.splitlines():
        match = re.fullmatch(r'(CONFIG_[A-Z0-9_]+)=(.*)', line)
        disabled = re.fullmatch(r'# (CONFIG_[A-Z0-9_]+) is not set', line)
        if match:
            key, value = match.groups()
        elif disabled:
            key, value = disabled[1], 'n'
        else:
            continue
        if key in options:
            raise ValueError('Duplicate kernel config option: ' + key)
        options[key] = value
    return options


def validate(product, record, image_gz, image, config):
    """Return attested mode; fail before the installer writes any managed file."""
    if product not in PRODUCTS:
        raise ValueError('Unknown r1 Android product: ' + str(product))
    if not 0 < len(image) <= MAX_IMAGE_BYTES:
        raise ValueError('Kernel Image size is outside the supported bound')
    for name, data in [('Image.gz', image_gz), ('Image', image), ('config', config)]:
        artifact = record.get('artifacts', {}).get(name, {})
        if artifact.get('bytes') != len(data) or artifact.get('sha256') != digest(data):
            raise ValueError('Kernel artifact record mismatch: ' + name)
    if record.get('config_sha256') != digest(config):
        raise ValueError('Kernel config record mismatch')
    decompressed, trailing = gzip_member(image_gz, len(image))
    if trailing or decompressed != image:
        raise ValueError('Image.gz does not encode the recorded kernel Image exactly')
    marker = b'IKCFG_ST'
    if image.count(marker) != 1:
        raise ValueError('Kernel must contain one unambiguous embedded IKCONFIG')
    offset = image.index(marker) + len(marker)
    embedded, trailing = gzip_member(image[offset:], MAX_CONFIG_BYTES)
    if not trailing.startswith(b'IKCFG_ED') or embedded != config:
        raise ValueError('Embedded kernel config differs from the recorded config')
    options = config_options(config)
    if options.get('CONFIG_IKCONFIG') != 'y':
        raise ValueError('Built-in IKCONFIG is required for prebuilt attestation')
    value = options.get(SOC_CONFIG, 'n')
    if value not in ('y', 'n'):
        raise ValueError('SOC must be built in or disabled, not a module or unknown value')
    enabled = value == 'y'
    if enabled != (product == 'lineage_r1_soc'):
        raise ValueError('Product/kernel SOC mismatch: select lineage_r1_soc only with ' +
                         SOC_CONFIG + '=y; lineage_r1 requires SOC disabled')
    if enabled and options.get('CONFIG_BATTERY_MT6357') != 'y':
        raise ValueError('Experimental startup requires the MT6357 gauge built in')
    return {'product': product, 'experimental_soc_health': enabled,
            'soc_config': value, 'config_sha256': digest(config),
            'image_sha256': digest(image), 'image_gz_sha256': digest(image_gz),
            'embedded_config_verified': True}


def makefile(mode):
    if mode['product'] not in PRODUCTS or mode['soc_config'] not in ('y', 'n'):
        raise ValueError('Invalid attested Health mode')
    return ('# Generated from the verified kernel; do not edit.\n'
            'override R1_INSTALLED_HEALTH_PRODUCT := ' + mode['product'] + '\n'
            'override R1_PREBUILT_SOC_CONFIG := ' + mode['soc_config'] + '\n'
            'override R1_PREBUILT_CONFIG_SHA256 := ' + mode['config_sha256'] + '\n').encode('ascii')
