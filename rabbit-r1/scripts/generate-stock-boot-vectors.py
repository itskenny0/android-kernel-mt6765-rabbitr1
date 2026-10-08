#!/usr/bin/env python3
"""Capture boot-record outputs by executing the verified stock HAL instructions."""
import hashlib
import importlib.util
from pathlib import Path
import sys

ROOT = Path('/rabbitr1')
spec = importlib.util.spec_from_file_location('stock_boot', Path(__file__).with_name('test-stock-boot-hal.py'))
stock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(stock)
image = (ROOT / 'research/android/stock-vendor/lib64/hw/android.hardware.boot@1.0-impl-1.2-mtkimpl.so').read_bytes()
assert hashlib.sha256(image).hexdigest() == stock.SHA
output = Path(sys.argv[1]).resolve()
assert output.is_relative_to(ROOT)
lines = ['# Stock HAL SHA256 ' + stock.SHA,
         '# action current slot before after; mark includes clearAvbbctlFlag']
emu = stock.Emulator(image)
for current in (0, 1):
    for slot in (0, 1):
        for value in (0, 0x0f, 0x7f, 0x80, 0xff):
            for action, address in [('active', 0xd638), ('unbootable', 0xd800)]:
                before = stock.control(value, value)
                emu.reset(before, current)
                assert emu.run(address, slot) == 1
                lines.append(f'{action} {current} {slot} {before.hex()} {emu.media.hex()}')
    for flag in (0, 1, 2, 255):
        for value in (0, 0x0f, 0x7f, 0x80, 0xff):
            before = stock.control(value, value, flag)
            emu.reset(before, current)
            assert emu.run(0xd4f8) == 1
            emu.run(0xb098)
            lines.append(f'mark {current} {current} {before.hex()} {emu.media.hex()}')
output.parent.mkdir(parents=True, exist_ok=True)
output.write_text('\n'.join(lines) + '\n')
print(f'Captured {len(lines)-2} stock instruction vectors in {output}')
