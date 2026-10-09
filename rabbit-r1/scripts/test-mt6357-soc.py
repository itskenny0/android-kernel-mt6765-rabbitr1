#!/usr/bin/env python3
"""Actual private MT6357 SOC driver plus byte-equal portable model, mocked hardware/core."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path('/rabbitr1')
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output',type=Path,default=Path('/rabbitr1/out/mt6357-soc'))
parser.add_argument('--project-root',type=Path,default=ROOT/'src/mainline/rabbit-r1')
parser.add_argument('--source',type=Path)
args=parser.parse_args()
PROJECT=args.project_root.resolve()
assert PROJECT.is_relative_to(ROOT)
args.source=args.source or PROJECT.parent/'drivers/power/supply/mt6357-gauge.c'
OUT=args.output.resolve()
assert OUT.is_relative_to('/rabbitr1') and args.source.resolve().is_relative_to('/rabbitr1')
OUT.mkdir(parents=True, exist_ok=True)
runner = PROJECT/'scripts/test-mt6357-live.py'
script = runner.read_text()
prefix = script[:script.index("fixture = PROJECT/'tests/battery/mt6357-live.c'")]
ns = {'__name__': '__soc_environment__', '__file__': str(runner)}
saved_argv = sys.argv
sys.argv = [str(runner), '--source', str(args.source.resolve()), '--output', str(OUT)]
try:
    exec(compile(prefix, str(runner), 'exec'), ns)
finally:
    sys.argv = saved_argv
replace = ns['replace_once']
prelude, body, model = ns['prelude'], ns['body'], ns['model']
prelude += '\n#include "'+str(PROJECT.parent/'drivers/power/supply/mt6357-r1-model/session.h')+'"\n'
prelude += r'''
#define S64_MAX INT64_MAX
#define WRITE_ONCE(x,v) mock_write_once(&(x),(v))
static void mock_write_once(bool *p, bool value);
#define system_freezable_power_efficient_wq ((void *)1)
#define DEFINE_SIMPLE_DEV_PM_OPS(name,suspend,resume) const int name=0
static bool board_r1=true;
static bool of_machine_is_compatible(const char *s) { assert(!strcmp(s,"rabbit,r1")); return board_r1; }
static unsigned int sleep_calls;
static void msleep(unsigned int ms);
static void *platform_get_drvdata(struct platform_device *p) { return p->dev.driver_data; }
'''
prelude = replace(prelude, 'struct delayed_work { struct work_struct work; };',
    'struct delayed_work { struct work_struct work; bool queued, running; unsigned long delay; };')
functions = {
'power_supply_changed': 'static void power_supply_changed(struct power_supply *p);',
'queue_delayed_work': 'static int queue_delayed_work(void *q, struct delayed_work *w, unsigned long delay);',
'mod_delayed_work': 'static int mod_delayed_work(void *q, struct delayed_work *w, unsigned long delay);',
'cancel_delayed_work_sync': 'static void cancel_delayed_work_sync(struct delayed_work *w);',
'devm_add_action_or_reset': 'static int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *data);',
'power_supply_reg_notifier': 'static int power_supply_reg_notifier(struct notifier_block *nb);',
'power_supply_unreg_notifier': 'static void power_supply_unreg_notifier(struct notifier_block *nb);',
'power_supply_get_property': 'static int power_supply_get_property(struct power_supply *p, enum power_supply_property prop, union power_supply_propval *v);',
'of_count_phandle_with_args': 'static int of_count_phandle_with_args(void *n, const char *p, void *a);',
'of_parse_phandle': 'static struct device_node *of_parse_phandle(void *n, const char *p, int i);',
'devm_power_supply_get_by_parent': 'static struct power_supply *devm_power_supply_get_by_parent(struct device *d, struct device *p);',
}
# The shared prelude's boundary stubs are single-line definitions.
import re
for name, declaration in functions.items():
    pattern = r'static [^\n]+\b'+name+r'\([^\n]*\) \{[^\n]*\}'
    matches = re.findall(pattern, prelude)
    assert len(matches)==1, name
    prelude = replace(prelude, matches[0], declaration)
model = 'static struct platform_device charger_platform;\nstatic struct power_supply charger;\nstatic struct device_node charger_node={99};\nstatic bool charger_present=true;\n'+model
model = replace(model, 'assert(node>=input_nodes && node<input_nodes+3 && node_refs);',
    'assert(node_refs); if (node == &charger_node) { if (step() || missing_provider) return NULL; provider_refs++; return &charger_platform; }\n    assert(node>=input_nodes && node<input_nodes+3);')
model = replace(model, 'device == &adc_provider || (self_provider && device == gauge.dev)',
    'device == &adc_provider || device == &charger_platform.dev || (self_provider && device == gauge.dev)')
model = replace(model, 'return false; /* STATUS has its own supplier suite. */', 'return charger_present;')
old = ns['ns']['function']('devm_power_supply_register', model).strip()
model = replace(model, old, r'''
static struct power_supply *devm_power_supply_register(struct device *dev,
    const struct power_supply_desc *desc, const struct power_supply_config *config)
{
    assert(config->drv_data == &gauge && config->fwnode == dev && desc == gauge.desc);
    assert(gauge.lock.initialized && gauge.regmap == &map && gauge.live_generation);
    int ret=step(); if (ret) return ERR_PTR(ret);
    if (fail_registration) return ERR_PTR(-EIO);
    if (gauge.soc.enabled) {
        assert(desc == &mt6357_gauge_soc_desc && desc->num_properties == 7);
        union power_supply_propval value={.intval=-777};
        assert(!desc->get_property(&psy,POWER_SUPPLY_PROP_CAPACITY,&value));
        assert(value.intval>=0 && value.intval<=100);
    }
    registrations++; return &psy;
}'''.strip())
# Descriptor is defined later; framework mock only checks property count here.
model = model.replace('desc == &mt6357_gauge_soc_desc && desc->num_properties == 7','desc->num_properties == 7')
body = replace(body, ns['model'], model)
fixture = PROJECT/'tests/battery/mt6357-soc.c'
live_fixture = (PROJECT/'tests/battery/mt6357-live.c').read_text()
live_fixture = live_fixture[:live_fixture.index('static int expected_charge')]
live_fixture = replace(live_fixture, 'unsigned int call=raw_calls++;', 'unsigned int call=(raw_calls++)%6;')
live_fixture = replace(live_fixture, 'unsigned int call=scale_calls++;', 'scale_calls++; unsigned int call=(raw_calls-1)%6;')
live_fixture = replace(live_fixture, 'static unsigned int cases, scale_cases;', 'static unsigned int adc_delay_us;')
live_fixture = replace(live_fixture, 'now+=17;', 'now+=17+adc_delay_us;')
# Production bodies stay unchanged. Hardware/core boundary implementations follow them.
code = prelude+ns['ns']['stock_defs']+body+ns['ns']['stock']+ns['setup']+live_fixture+fixture.read_text()
path = OUT/'host.c'
path.write_text(code)
models = PROJECT.parent/'drivers/power/supply/mt6357-r1-model'
for name in ('profile.c','profile.h','stock_profiles.c','session.c','session.h'):
    assert (models/name).read_bytes() == (PROJECT/'battery'/name).read_bytes(), name
command = ['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
           '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-const-variable',
           '-DCONFIG_BATTERY_MT6357_R1_SOC=1','-pthread','-fsanitize=address,undefined',
           '-fno-omit-frame-pointer','-fno-pie','-no-pie',str(path),
           *[str(models/f) for f in ('profile.c','stock_profiles.c','session.c')],'-o',str(OUT/'host')]
results=[]
commands=[]
for diagnostics in (0,1):
    cmd=command[:-2]+[f'-DCONFIG_BATTERY_MT6357_LIVE_DIAGNOSTICS={diagnostics}', '-o',str(OUT/f'host-{diagnostics}')]
    commands.append(cmd)
    subprocess.run(cmd,check=True)
    result=subprocess.run([str(OUT/f'host-{diagnostics}')],check=False,text=True,capture_output=True,timeout=90)
    (OUT/f'run-{diagnostics}.log').write_text(result.stdout+result.stderr)
    if result.returncode:
        print(result.stderr,file=sys.stderr)
        result.check_returncode()
    results.append(result)
assert results[0].stdout == results[1].stdout
result=results[0]
(OUT/'run.log').write_text(result.stdout+result.stderr)
print(result.stdout,end='')
print(result.stderr,end='',file=sys.stderr)
result.check_returncode()
# Independently evaluate the physical test grid using exact rational stock
# profile geometry. No C output is used to derive roots, cutoff or percentages.
from fractions import Fraction as F
oracle_ns={'__name__':'__fraction_reference__'}
exec(compile((PROJECT/'battery/tests/model-reference.py').read_text(),'model-reference.py','exec'),oracle_ns)
profiles=json.loads((PROJECT/'battery/tests/stock-oracles.json').read_text())['temperature_profiles']
points=next(p['profile'] for p in profiles if p['temp_c']==25)
cut_error, cut=oracle_ns['cutoff'](points,33500,(-5000,100,75,100))
assert cut_error==0 and cut[-1] != 2
usable=(cut[0]*100-30300,cut[0]*100+100+30300)
ceil=lambda x: -((-x.numerator)//x.denominator)
round_down=lambda x: x.numerator//x.denominator
seen_zero=seen_full=False
for line in result.stdout.splitlines():
    if not line.startswith('GRID '): continue
    mv,ret,capacity,qlo,qhi,ulo,uhi,bp,accepted,elapsed,margin=map(int,line.split()[1:])
    error,root=oracle_ns['seed'](points,mv*10,(0,100,75,100))
    if error:
        assert ret<0 and capacity==-777,(mv,error,line)
        continue
    padding=30300+ceil(F(10299600*elapsed,3600000000000))
    q=(root[0]*100-padding,root[0]*100+100+padding)
    assert (qlo,qhi)==q and (ulo,uhi)==usable,line
    midq=F(sum(q),2).__floor__(); midu=F(sum(usable),2).__floor__()
    expected_bp=max(0,min(10000,round_down(F(10000*(midu-midq),midu))))
    assert bp==expected_bp,line
    age=margin-accepted; widening=ceil(F(10299600*age,3600000000000))
    wide=(q[0]-widening,q[1]+widening)
    ratios=[F(10000*(u-v),u) for v in wide for u in usable]
    allowed=wide[1]-wide[0]<=101000 and ceil(max(ratios))-round_down(min(ratios))<=2000
    assert (ret==0)==allowed,(mv,ret,wide,ratios)
    if not ret:
        assert capacity==(expected_bp+50)//100,line
        seen_zero |= capacity==0
        seen_full |= capacity==100
assert seen_zero and seen_full,'Grid must exercise genuine modeled zero and full'

(OUT/'result.json').write_text(json.dumps({'passed':True,'hardware_tested':False,
    'commands':commands,'stdout':result.stdout,'stderr':result.stderr,
    'sources':{str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in [ns['source'],fixture,Path(__file__),path,runner,PROJECT/'scripts/test-mt6357-current.py',PROJECT/'battery/tests/model-reference.py',PROJECT/'battery/tests/stock-oracles.json',*models.glob('*')]},
    'scope':'Actual complete gauge production body, shared real reducer/model; mocked Linux workqueue/PM/devres, PMIC and IIO. No hardware accuracy or scheduler-time guarantee.'},indent=2)+'\n')
