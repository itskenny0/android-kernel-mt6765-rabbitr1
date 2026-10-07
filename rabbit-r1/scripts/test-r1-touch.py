#!/usr/bin/env python3
"""Check the CST836 draft wiring against the extracted stock DT and GPIO table."""
import importlib.util
from pathlib import Path
import struct
import sys

ROOT = Path('/rabbitr1')
spec = importlib.util.spec_from_file_location('validate', ROOT/'scripts/validate-kernel.py')
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)
dtb = ROOT/'dist/mainline/mt6765-rabbit-r1.dtb'
if len(sys.argv) == 2:
    dtb = Path(sys.argv[1]).resolve()
    if not dtb.is_relative_to(ROOT):
        raise SystemExit('DTB must be under /rabbitr1')
nodes = validate.fdt_nodes(dtb.read_bytes())
stock = validate.fdt_nodes((ROOT/'firmware/stock-v0.8.293/merged.dtb').read_bytes())


def cells(value):
    assert len(value) % 4 == 0
    return struct.unpack('>' + 'I' * (len(value) // 4), value)


bus = nodes['/soc/i2c@11011000']
old_bus = stock['/i2c4@11011000']
assert bus['status'] == b'disabled\0', 'Do not enable I2C4 before channel routing is implemented'
assert bus['reg'] == old_bus['reg']
assert bus['interrupts'] == old_bus['interrupts']
assert 'mediatek,use-push-pull' not in bus
assert 'mediatek,use-open-drain' in old_bus
assert cells(bus['clock-frequency']) == (100000,)
assert cells(old_bus['clock-frequency']) == (400000,)
assert cells(old_bus['ch_offset_default']) == (0x100,)
assert nodes['/aliases']['i2c4'] == b'/soc/i2c@11011000\0'
touch = nodes['/soc/i2c@11011000/touchscreen@15']
old_touch = stock['/i2c4@11011000/hynitron@1a']
assert touch['reg'] == old_touch['reg'] == struct.pack('>I',0x15)
assert touch['compatible'] == b'hynitron,cst836\0'
assert touch['touchscreen-size-x'] + touch['touchscreen-size-y'] == old_touch['hynitron,display-coords']
pio = nodes['/soc/pin-controller@10005000']['phandle']
assert touch['interrupt-parent'] == pio
assert cells(touch['interrupts']) == (cells(old_touch['hynitron,irq-gpio'])[1],2)
assert touch['reset-gpios'][:4] == pio
# The vendor uses physical 0 -> 1; the GPIO descriptor uses active-low semantics.
assert cells(touch['reset-gpios'])[1:] == (cells(old_touch['hynitron,reset-gpio'])[1],1)
assert cells(old_touch['hynitron,max-touch-number']) == (2,)
by_phandle = {n['phandle']: path for path,n in nodes.items() if 'phandle' in n}
table = stock['/gpio@10005000']['gpio_init_default']
for node,child,pins in [(bus,'pins-bus',[105,106]),
                        (touch,'pins-irq',[0]),(touch,'pins-reset',[174])]:
    path = by_phandle[node['pinctrl-0']] + '/' + child
    for mux,pin in zip(cells(nodes[path]['pinmux']),pins,strict=True):
        row = struct.unpack_from('>7I',table,pin*28)
        assert row[0] == pin and mux == (pin << 8) | row[1]
print('PASS: r1 touch address, resolution, IRQ/reset and I2C4 pin muxes match stock')
print('I2C4 remains disabled; no CST836 silicon or bus operation is proven by this check.')
