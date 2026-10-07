#!/usr/bin/env python3
"""Compare compiled r1 power-device wiring with the verified stock device tree."""
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


bus = nodes['/soc/i2c@11016000']
old_bus = stock['/i2c5@11016000']
assert bus['status'] == b'okay\0'
assert bus['reg'] == old_bus['reg']
assert bus['interrupts'] == old_bus['interrupts']
assert 'mediatek,use-push-pull' in bus and 'mediatek,use-push-pull' in old_bus
assert cells(bus['clock-frequency']) == (100000,)
assert cells(old_bus['clock-frequency'])[0] >= cells(bus['clock-frequency'])[0]
assert cells(bus['clock-div']) == (1,)
assert nodes['/aliases']['i2c5'] == b'/soc/i2c@11016000\0'

gpio_table = stock['/gpio@10005000']['gpio_init_default']
assert len(gpio_table) == 180 * 7 * 4
by_phandle = {n['phandle']: n for n in nodes.values() if 'phandle' in n}
bus_pins = by_phandle[bus['pinctrl-0']]
# Pin groups themselves have no properties other than phandle; check their child.
pin_path = next(path for path, node in nodes.items() if node is bus_pins)
pinmux = cells(nodes[pin_path+'/pins-bus']['pinmux'])
for mux, pin in zip(pinmux, [48, 49], strict=True):
    row = struct.unpack_from('>7I', gpio_table, pin * 28)
    assert row[0] == pin and mux == (pin << 8) | row[1]

pmic_path = '/soc/i2c@11016000/pmic@34'
pmic = nodes[pmic_path]
old_pmic = stock['/mt6370_pmu_dts']
assert pmic['compatible'] == b'mediatek,mt6370\0'
assert pmic['reg'] == stock['/i2c5@11016000/subpmic_pmu@34']['reg']
pio = nodes['/soc/pin-controller@10005000']['phandle']
assert pmic['interrupts-extended'][:4] == pio
assert cells(pmic['interrupts-extended'])[1:] == cells(stock['/mt6370_pmu_eint']['interrupts'])[:2]
assert cells(pmic['interrupts-extended'])[1] == cells(old_pmic['mt6370,intr_gpio_num'])[0]
assert cells(pmic['#interrupt-cells']) == (1,)
assert 'interrupt-controller' in pmic
irq_group = by_phandle[pmic['pinctrl-0']]
irq_path = next(path for path, node in nodes.items() if node is irq_group)
assert cells(nodes[irq_path+'/pins-irq']['pinmux']) == (11 << 8,)

adc = nodes[pmic_path+'/adc']
assert adc['compatible'] == b'mediatek,mt6370-adc\0'
assert adc.get('status', b'okay\0') == b'okay\0'
assert cells(adc['#io-channel-cells']) == (1,)
# Current, voltage and chip temperature only; TS_BAT is not a battery thermometer.
channels = nodes['/power-monitor']['io-channels']
assert len(channels) == 6 * 8
for index, channel in enumerate([0, 2, 3, 5, 6, 8]):
    assert channels[index*8:index*8+4] == adc['phandle']
    assert cells(channels[index*8+4:index*8+8]) == (channel,)
assert nodes['/power-monitor']['compatible'] == b'iio-hwmon\0'

# Explicit disabled children prevent the MFD core from creating live devices.
for child in ['charger', 'backlight', 'tcpc', 'indicator', 'flashlight']:
    assert nodes[pmic_path+'/'+child]['status'] == b'disabled\0', child
assert nodes[pmic_path+'/regulators'] == {}, 'Unexpected rail constraints during power bring-up'
charger = nodes[pmic_path+'/charger']
assert charger['io-channels'] == adc['phandle'] + struct.pack('>I', 5)
assert cells(charger['interrupts']) == (68, 48, 6)
assert charger['interrupt-names'].split(b'\0')[:-1] == [b'uvp_d_evt', b'attach_i', b'mivr']
tcpc = nodes[pmic_path+'/tcpc']
assert tcpc['interrupts-extended'][:4] == pio
assert cells(tcpc['interrupts-extended'])[1:] == cells(stock['/mt6370_pd_eint']['interrupts'])[:2]
backlight = nodes[pmic_path+'/backlight']
old_backlight = stock['/mt6370_pmu_dts/bled']
assert backlight['mediatek,bled-channel-use'] == bytes(cells(old_backlight['mt,chan_en']))
assert backlight['max-brightness'] == old_backlight['mt,max_bled_brightness']
assert cells(backlight['default-brightness']) == (0,)
assert cells(backlight['mediatek,bled-ovp-microvolt']) == (17000000 + 4000000 * cells(old_backlight['mt,bl_ovp_level'])[0],)
assert cells(backlight['mediatek,bled-ocp-microamp']) == (900000 + 300000 * cells(old_backlight['mt,bl_ocp_level'])[0],)
print('PASS: r1 I2C5 pins/mode/address/IRQs match stock; ADC/hwmon channels and child gates checked')
print('No bus transfers performed; this does not establish charging or battery-temperature support.')
