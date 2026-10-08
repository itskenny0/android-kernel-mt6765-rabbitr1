#!/usr/bin/env python3
"""Compare compiled r1 power-device wiring with the verified stock device tree."""
import importlib.util
import hashlib
import json
from pathlib import Path
import re
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


gauge = nodes['/soc/pwrap@1000d000/pmic/gauge']
old_gauge = stock['/pwrap@1000d000/main_pmic/mtk_gauge']
assert gauge['compatible'] == old_gauge['compatible'] == b'mediatek,mt6357-gauge\0'
assert gauge.get('status', b'okay\0') == b'okay\0'
# Stock DT shunt is in mOhm and current calibration is a percentage. The stock
# parser multiplies each by ten for its internal units; do not copy the raw cells.
assert cells(gauge['shunt-resistor-micro-ohms']) == (cells(old_gauge['R_FG_VALUE'])[0] * 1000,)
assert cells(gauge['mediatek,current-gain-permille']) == (cells(old_gauge['CAR_TUNE_VALUE'])[0] * 10,)
assert cells(gauge['mediatek,thermistor-pullup-ohms']) == cells(old_gauge['RBAT_PULL_UP_R'])
# Stock compensation uses 0.1 mOhm units internally, with COM_R_FG_VALUE
# multiplied by ten in the parser. Both measured voltages get the correction.
series = cells(old_gauge['COM_FG_METER_RESISTANCE'])[0] * 100
series += cells(old_gauge['COM_R_FG_VALUE'])[0] * 1000
assert cells(gauge['mediatek,thermistor-series-micro-ohms']) == (series,)
assert cells(old_gauge.get('NO_BAT_TEMP_COMPENSATE', b'\0\0\0\0')) == (0,)
pmic_adc = nodes['/soc/pwrap@1000d000/pmic/adc']
old_adc = stock['/pwrap@1000d000/main_pmic/pmic_auxadc']
assert pmic_adc['compatible'] == b'mediatek,mt6357-auxadc\0'
assert pmic_adc.get('status', b'okay\0') == b'okay\0'
assert cells(pmic_adc['#io-channel-cells']) == (1,)
assert gauge['io-channel-names'].split(b'\0')[:-1] == [
    b'battery-voltage', b'battery-thermistor', b'thermistor-reference']
assert cells(gauge['io-channels']) == tuple(
    v for channel in (1, 3, 13) for v in (cells(pmic_adc['phandle'])[0], channel))
old_names = old_gauge['io-channel-names'].split(b'\0')[:-1]
old_inputs = cells(old_gauge['io-channels'])
old_channels = dict(zip(old_names, zip(old_inputs[::2], old_inputs[1::2]), strict=True))
for name, channel in ((b'pmic_battery_voltage', 1), (b'pmic_battery_temp', 3), (b'pmic_bif_voltage', 14)):
    assert old_channels[name] == (cells(old_adc['phandle'])[0], channel)
# VBIF's binding ID changed from vendor 14 to mainline 13. ISENSE and
# BAT_TEMP retain their IDs. Never copy vendor IDs by array position.
table_path = 'src/kernel/drivers/power/supply/mtk_battery_table.h'
table_data = (ROOT/table_path).read_bytes()
pin = json.loads((ROOT/'sources.lock.json').read_text())['ci_files'][table_path]
assert len(table_data) == pin['bytes'] and hashlib.sha256(table_data).hexdigest() == pin['sha256']
table_header = table_data.decode()
assert re.search(r'^#define BAT_NTC_10\s+1$', table_header, re.M)
table_body = re.search(r'Fg_Temperature_Table\[21\] = \{(.*?)\n};', table_header, re.S)[1]
points = [tuple(map(int, row)) for row in re.findall(r'\{(-?\d+),\s*(\d+)\}', table_body)]
assert len(points) == 21
expected = b''.join(struct.pack('>iI', temperature * 1000, ohms) for temperature, ohms in points)
assert gauge['mediatek,thermistor-resistance-table'] == expected
stock_table = b''.join(struct.pack('<ii', temperature, ohms) for temperature, ohms in points)
stock_image = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
assert stock_image.count(stock_table) == 1, 'Stock NTC table does not match the shipped kernel'

bus = nodes['/soc/i2c@11016000']
old_bus = stock['/i2c5@11016000']
assert bus['status'] == b'okay\0'
assert bus['reg'] == old_bus['reg']
assert bus['interrupts'] == old_bus['interrupts']
assert 'mediatek,use-push-pull' in bus and 'mediatek,use-push-pull' in old_bus
assert cells(bus['clock-frequency']) == (100000,)
assert cells(old_bus['clock-frequency'])[0] >= cells(bus['clock-frequency'])[0]
# MT6765 programs this divider in the timing register, as the stock driver does.
assert bus['clock-div'] == old_bus['clock-div'] == struct.pack('>I', 5)
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
for child in ['backlight', 'tcpc', 'indicator', 'flashlight']:
    assert nodes[pmic_path+'/'+child]['status'] == b'disabled\0', child
assert nodes[pmic_path+'/regulators'] == {}, 'Unexpected rail constraints during power bring-up'
charger = nodes[pmic_path+'/charger']
assert charger['status'] == b'okay\0'
assert 'richtek,managed-charging' in charger
policy = nodes['/charging-policy']
assert policy['charger-supply'] == charger['phandle']
assert policy['usb-gadget-supply'] == nodes['/soc/usb@11200000']['phandle']
assert policy['battery-supply'] == nodes['/soc/pwrap@1000d000/pmic/gauge']['phandle']
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
print('PASS: MT6357 gauge current/temperature calibration, ADC wiring and NTC table match stock')
print('No bus transfers performed; charging and physical measurement accuracy remain untested.')
