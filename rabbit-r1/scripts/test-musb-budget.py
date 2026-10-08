#!/usr/bin/env python3
"""Exercise native MediaTek gadget-budget callbacks and their resource lifetime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
OUT = ROOT/'out/musb-budget'
OUT.mkdir(parents=True, exist_ok=True)
os.environ['TMPDIR'] = str(ROOT/'.tmp')
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/usb/musb/mediatek.c')
args = parser.parse_args()
source = args.source.resolve()
if not source.is_relative_to(ROOT):
    raise SystemExit('Source must stay under /rabbitr1')
s = source.read_text()
gadget = (SRC/'drivers/usb/musb/musb_gadget.c').read_text()
core = (SRC/'drivers/usb/musb/musb_core.h').read_text()


def function(name, text=s):
    match = re.search(r'static [\w *]+\b'+name+r'\([^;]*?\)\n\{', text)
    assert match, name
    return text[match.start():text.index('\n}', match.end())+2]+'\n'


power = s[s.index('#if IS_ENABLED(CONFIG_USB_MUSB_MEDIATEK_POWER_SUPPLY)'):s.index('\nstruct mtk_glue {')]
assert '.vbus_draw = mtk_musb_vbus_draw,' in s
assert '.shutdown = mtk_musb_shutdown,' in s
assert '(*vbus_draw)(struct musb *musb, unsigned int mA)' in core
probe = function('mtk_musb_probe')
assert probe.index('mtk_musb_power_init(') < probe.index('platform_device_register_full(')
init = function('mtk_musb_init')
assert 'mtk_musb_power_role(&glue->power, glue->role);' in init
role = function('mtk_otg_switch_set')
assert role.count('mtk_musb_power_role(&glue->power, USB_ROLE_NONE);') == 3
assert 'mtk_musb_power_role(&glue->power, new_role);' in role
prelude = r'''
#include <assert.h>
#include <errno.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdatomic.h>
typedef uint64_t u64;
typedef uint8_t u8;
#define EPROBE_DEFER 517
#define IS_ENABLED(x) BUDGET_ENABLED
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define GFP_KERNEL 0
#define MTK_MUSB_CLKS_NUM 3
#define IS_ERR(x) ((uintptr_t)(x) >= (uintptr_t)-4095)
#define PTR_ERR(x) ((int)(intptr_t)(x))
#define ERR_PTR(x) ((void *)(intptr_t)(x))
enum usb_role { USB_ROLE_NONE, USB_ROLE_HOST, USB_ROLE_DEVICE };
enum phy_mode { PHY_MODE_USB_HOST, PHY_MODE_USB_DEVICE, PHY_MODE_USB_OTG };
enum { OTG_STATE_A_WAIT_VRISE, OTG_STATE_B_IDLE, MUSB_DEVCTL=0, MUSB_DEVCTL_SESSION=1 };
struct phy { int unused; };
struct usb_otg { int state; };
struct usb_phy { struct usb_otg *otg; };
#define MUSB_HST_MODE(m) ((m)->is_host=true)
#define MUSB_DEV_MODE(m) ((m)->is_host=false)
#define dev_err(...) ((void)0)
enum power_supply_property { POWER_SUPPLY_PROP_ONLINE, POWER_SUPPLY_PROP_CURRENT_MAX, POWER_SUPPLY_PROP_SCOPE };
enum { POWER_SUPPLY_SCOPE_DEVICE=2, POWER_SUPPLY_TYPE_USB=4 };
union power_supply_propval { int intval; };
struct power_supply;
struct power_supply_desc {
 const char *name;
 int type;
 const enum power_supply_property *properties;
 unsigned int num_properties;
 int (*get_property)(struct power_supply *,enum power_supply_property,union power_supply_propval *);
 int (*set_property)(struct power_supply *,enum power_supply_property,const union power_supply_propval *);
 int (*property_is_writeable)(struct power_supply *,enum power_supply_property);
};
struct power_supply_config { void *drv_data, *fwnode; };
struct power_supply { const struct power_supply_desc *desc; void *data; };
struct device { void *data, *fwnode; struct device *parent; };
struct platform_device { struct device dev; };
static struct power_supply supply;
static bool supply_live, notified;
static unsigned int changes, registration_calls;
static int allocation_error, registration_error, action_error;
static void (*managed_action)(void *);
static void *managed_data;
static _Thread_local unsigned int held;
typedef struct { pthread_mutex_t raw; } spinlock_t;
static void spin_lock_init(spinlock_t *s) { assert(!pthread_mutex_init(&s->raw,NULL)); }
static void host_lock(spinlock_t *s) { assert(!held); assert(!pthread_mutex_lock(&s->raw)); held++; }
static void host_unlock(spinlock_t *s) { assert(held == 1); held--; assert(!pthread_mutex_unlock(&s->raw)); }
#define spin_lock_irqsave(p,flags) do { flags=0; host_lock(p); } while (0)
#define spin_unlock_irqrestore(p,flags) do { (void)(flags); host_unlock(p); } while (0)
static void *power_supply_get_drvdata(struct power_supply *p) { assert(supply_live); return p->data; }
static void *dev_fwnode(struct device *dev) { return dev->fwnode; }
static const char *dev_name(struct device *dev) { return "11200000.usb"; }
static void *dev_get_drvdata(struct device *dev) { return dev->data; }
static void *platform_get_drvdata(struct platform_device *dev) { return dev->dev.data; }
static char *devm_kasprintf(struct device *dev,int flag,const char *format,const char *name)
{ static char result[80]; if (allocation_error) return NULL; snprintf(result,sizeof(result),format,name); return result; }
static void power_supply_changed(struct power_supply *p)
{ assert(held && supply_live && p == &supply); changes++; notified=true; }
static struct power_supply *devm_power_supply_register(struct device *dev,const struct power_supply_desc *desc,const struct power_supply_config *config)
{
 registration_calls++;
 assert(!held && !supply_live && config->fwnode == dev->fwnode);
 if (registration_error) return ERR_PTR(registration_error);
 supply_live=true; supply=(struct power_supply){.desc=desc,.data=config->drv_data};
 assert(!strcmp(desc->name,"11200000.usb-gadget") && desc->type == POWER_SUPPLY_TYPE_USB);
 assert(desc->num_properties == 3 && !desc->set_property && !desc->property_is_writeable);
 /* Model callbacks during publication, before the caller has the handle. */
 for (unsigned int i=0; i<desc->num_properties; i++) {
  union power_supply_propval v={.intval=-1}; enum power_supply_property prop=desc->properties[i];
  assert(!desc->get_property(&supply,prop,&v));
  assert(v.intval == (prop == POWER_SUPPLY_PROP_SCOPE ? POWER_SUPPLY_SCOPE_DEVICE : 0));
 }
 return &supply;
}
static int devm_add_action_or_reset(struct device *d,void (*action)(void *),void *arg)
{
 assert(supply_live && !managed_action);
 if (action_error) { action(arg); return action_error; }
 managed_action=action; managed_data=arg; return 0;
}
'''
body = power+r'''
struct mtk_glue {
 struct mtk_musb_power power;
 struct platform_device *musb_pdev, *usb_phy;
 struct musb *musb;
 struct phy *phy;
 enum phy_mode phy_mode;
 enum usb_role role;
 int clks[3];
};
struct musb;
struct musb_platform_ops { int (*vbus_draw)(struct musb *,unsigned int); };
struct usb_gadget { struct musb *musb; };
struct musb { struct device *controller; struct usb_phy *xceiv; const struct musb_platform_ops *ops; struct usb_gadget g; u8 *mregs; bool is_host; };
#define gadget_to_musb(g) ((g)->musb)
static int usb_phy_set_power(void *,unsigned int);
static u8 readb(void *);
static void musb_writeb(void *,int,u8);
static int phy_power_on(struct phy *);
static int phy_power_off(struct phy *);
static int phy_set_mode(struct phy *,enum phy_mode);
static void phy_exit(struct phy *);
static void mtk_otg_switch_exit(struct mtk_glue *);
static void clk_bulk_disable_unprepare(int,void *);
static int pm_runtime_put_sync(struct device *);
static void pm_runtime_disable(struct device *);
static void platform_device_unregister(struct platform_device *);
static void usb_phy_generic_unregister(struct platform_device *);
'''
for name in ('mtk_otg_switch_set','mtk_musb_exit','mtk_musb_vbus_draw','mtk_musb_remove','mtk_musb_shutdown'):
    body += function(name)
body += function('musb_gadget_vbus_draw',gadget)
checks = r'''
static struct mtk_glue glue;
static struct platform_device parent, child, phy;
static struct musb musb;
static struct phy hardware_phy;
static struct usb_otg otg;
static struct usb_phy xceiver={.otg=&otg};
static u8 devctl;
static const struct musb_platform_ops mtk_ops={.vbus_draw=mtk_musb_vbus_draw}, legacy_ops={0};
static atomic_uint phy_calls;
static _Thread_local int phy_error, interleave;
static _Thread_local unsigned int expected_at_phy;
static unsigned int child_releases, phy_releases;
static int get(enum power_supply_property prop)
{
 union power_supply_propval v={.intval=-7};
 assert(!supply.desc->get_property(&supply,prop,&v)); return v.intval;
}
static int draw(unsigned int ma) { return musb_gadget_vbus_draw(&musb.g,ma); }
static u8 readb(void *addr) { assert(addr == &devctl); return devctl; }
static void musb_writeb(void *addr,int off,u8 value)
{
 assert(addr == &devctl && off == MUSB_DEVCTL);
#if BUDGET_ENABLED
 assert(!get(POWER_SUPPLY_PROP_CURRENT_MAX));
#endif
 devctl=value;
}
static int phy_power_on(struct phy *p) { assert(p == &hardware_phy); return 0; }
static int phy_power_off(struct phy *p) { assert(p == &hardware_phy); return 0; }
static int phy_set_mode(struct phy *p,enum phy_mode mode) { assert(p == &hardware_phy); return 0; }
static void phy_exit(struct phy *p) { assert(p == &hardware_phy); }
static void mtk_otg_switch_exit(struct mtk_glue *g)
{
 assert(g == &glue);
#if BUDGET_ENABLED
 assert(!get(POWER_SUPPLY_PROP_CURRENT_MAX));
#endif
}
static void clk_bulk_disable_unprepare(int count,void *clks) { assert(count == 3 && clks == glue.clks); }
static int pm_runtime_put_sync(struct device *dev) { assert(dev == &child.dev); return 0; }
static void pm_runtime_disable(struct device *dev) { assert(dev == &child.dev); }
static void deferred_notification(void)
{
 if (!notified) return;
 /* The real power-supply core queues a notification, not a saved current. */
 assert(!held && supply_live);
 int ua=get(POWER_SUPPLY_PROP_CURRENT_MAX); assert(ua>=0 && ua<=500000);
 assert(get(POWER_SUPPLY_PROP_ONLINE) == !!ua); notified=false;
}
static pthread_mutex_t gate=PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t cond=PTHREAD_COND_INITIALIZER;
static bool entered, released;
static int usb_phy_set_power(void *x,unsigned int ma)
{
 assert(!held && x == &xceiver);
 atomic_fetch_add(&phy_calls,1);
#if BUDGET_ENABLED
 if (musb.ops == &mtk_ops) assert(get(POWER_SUPPLY_PROP_CURRENT_MAX) == (int)expected_at_phy*1000);
 int action=interleave; interleave=0;
 if (action>=1 && action<=3) {
  unsigned int next=action == 1 ? 2 : action == 2 ? 0 : 500;
  expected_at_phy=next<expected_at_phy ? next : expected_at_phy;
  assert(!draw(next));
 } else if (action == 4) {
  assert(!mtk_otg_switch_set(&glue,USB_ROLE_HOST));
 } else if (action == 5) {
  assert(!mtk_otg_switch_set(&glue,USB_ROLE_NONE));
  assert(!mtk_otg_switch_set(&glue,USB_ROLE_DEVICE));
 } else if (action == 6) {
  mtk_musb_shutdown(&parent);
 } else if (action == 8) {
  assert(!mtk_musb_exit(&musb));
 } else if (action == 7) {
  assert(!pthread_mutex_lock(&gate)); entered=true; assert(!pthread_cond_broadcast(&cond));
  while (!released) assert(!pthread_cond_wait(&cond,&gate));
  assert(!pthread_mutex_unlock(&gate));
 }
#endif
 return phy_error;
}
static void platform_device_unregister(struct platform_device *dev)
{
 assert(dev == &child && !child_releases && !phy_releases);
#if BUDGET_ENABLED
 assert(glue.power.stopped && !get(POWER_SUPPLY_PROP_CURRENT_MAX));
 unsigned int before=atomic_load(&phy_calls);
 assert(draw(500) == -ESHUTDOWN && draw(0) == -ESHUTDOWN);
 assert(atomic_load(&phy_calls) == before);
#endif
 child_releases++;
}
static void usb_phy_generic_unregister(struct platform_device *dev)
{ assert(dev == &phy && child_releases == 1 && !phy_releases); phy_releases++; }
static void setup(void)
{
 assert(!supply_live && !managed_action);
 memset(&glue,0,sizeof(glue)); parent.dev.data=&glue; parent.dev.fwnode=&parent;
 child.dev.parent=&parent.dev; glue.musb_pdev=&child; glue.usb_phy=&phy;
 musb=(struct musb){.controller=&child.dev,.xceiv=&xceiver,.ops=&mtk_ops,.mregs=&devctl}; musb.g.musb=&musb;
 glue.phy=&hardware_phy; glue.musb=&musb; glue.role=USB_ROLE_DEVICE; devctl=0;
 allocation_error=registration_error=action_error=phy_error=interleave=0;
 changes=registration_calls=child_releases=phy_releases=0; notified=false; atomic_store(&phy_calls,0);
}
static void cleanup(void)
{
 if (managed_action) { managed_action(managed_data); managed_action=NULL; }
 if (supply_live) {
#if BUDGET_ENABLED
  assert(glue.power.stopped);
#endif
  deferred_notification(); supply_live=false;
 }
#if BUDGET_ENABLED
 assert(!pthread_mutex_destroy(&glue.power.lock.raw));
#endif
}
static void start(void)
{ setup(); assert(!mtk_musb_power_init(&parent.dev,&glue.power)); mtk_musb_power_role(&glue.power,USB_ROLE_DEVICE); }
static void *delayed_draw(void *unused)
{ interleave=7; expected_at_phy=100; phy_error=0; assert(draw(500) == -ECANCELED); return NULL; }
int main(void)
{
#if BUDGET_ENABLED
 unsigned int requests=0, invalid=0, faults=0, races=0;
 start(); assert(!get(POWER_SUPPLY_PROP_CURRENT_MAX) && !get(POWER_SUPPLY_PROP_ONLINE));
 for (unsigned int ma=0; ma<=500; ma++) {
  unsigned int earlier=changes;
  expected_at_phy=ma ? ma-1 : 0;
  assert(!draw(ma) && get(POWER_SUPPLY_PROP_CURRENT_MAX) == (int)ma*1000);
  assert(changes == earlier+(ma != 0));
  deferred_notification();
  unsigned int before=changes; expected_at_phy=ma;
  assert(!draw(ma) && changes == before); requests+=2;
 }
 const unsigned int events[]={2,0,8,100,500,100,2,500,0};
 unsigned int previous=500;
 for (unsigned int i=0; i<ARRAY_SIZE(events); i++) {
  expected_at_phy=events[i]<previous ? events[i] : previous;
  assert(!draw(events[i])); previous=events[i];
  assert(get(POWER_SUPPLY_PROP_CURRENT_MAX) == (int)previous*1000); requests++;
 }
 const unsigned int bad[]={501,900,INT_MAX,UINT_MAX};
 for (unsigned int i=0; i<ARRAY_SIZE(bad); i++) {
  expected_at_phy=0; assert(!draw(500)); unsigned int before=atomic_load(&phy_calls);
  assert(draw(bad[i]) == -ERANGE && !get(POWER_SUPPLY_PROP_CURRENT_MAX));
  assert(atomic_load(&phy_calls) == before); invalid++;
 }
 for (unsigned int mode=0; mode<2; mode++) {
  assert(!mtk_otg_switch_set(&glue,mode ? USB_ROLE_HOST : USB_ROLE_NONE));
  unsigned int before=atomic_load(&phy_calls);
  assert(draw(100) == -ENOTCONN && !get(POWER_SUPPLY_PROP_CURRENT_MAX));
  assert(atomic_load(&phy_calls) == before); invalid++;
  expected_at_phy=0; assert(!draw(0));
  assert(!mtk_otg_switch_set(&glue,USB_ROLE_DEVICE));
  assert(!get(POWER_SUPPLY_PROP_CURRENT_MAX));
 }
 for (unsigned int ma=0; ma<=500; ma+=100) {
  expected_at_phy=0; assert(!draw(500));
  phy_error=-EIO; expected_at_phy=ma;
  assert(draw(ma) == -EIO && !get(POWER_SUPPLY_PROP_CURRENT_MAX));
  phy_error=0; faults++;
 }
 expected_at_phy=0; assert(!draw(100)); unsigned int role_changes=changes;
 assert(mtk_otg_switch_set(&glue,(enum usb_role)99) == -EINVAL); invalid++;
 assert(!mtk_otg_switch_set(&glue,USB_ROLE_DEVICE));
 assert(changes == role_changes && get(POWER_SUPPLY_PROP_CURRENT_MAX) == 100000);
 union power_supply_propval invalid_value={.intval=123};
 assert(supply.desc->get_property(&supply,(enum power_supply_property)99,&invalid_value) == -EINVAL && invalid_value.intval == 123);
 cleanup();
 const unsigned int actions[]={1,2,3,4,5,6,8};
 for (unsigned int i=0; i<ARRAY_SIZE(actions); i++) {
  unsigned int action=actions[i];
  start(); expected_at_phy=0; assert(!draw(100));
  expected_at_phy=100; interleave=action;
  assert(draw(500) == (action == 6 ? -ESHUTDOWN : -ECANCELED));
  unsigned int want=action == 1 ? 2000 : action == 3 ? 500000 : 0;
  assert(get(POWER_SUPPLY_PROP_CURRENT_MAX) == (int)want);
  if (action == 8) {
   assert(draw(100) == -ENOTCONN);
   mtk_musb_power_role(&glue.power,USB_ROLE_DEVICE);
   assert(!get(POWER_SUPPLY_PROP_CURRENT_MAX));
   expected_at_phy=0; assert(!draw(100));
  }
  deferred_notification(); cleanup(); races++;
 }
 /* A later suspend request completes while an older increase is still in PHY. */
 start(); expected_at_phy=0; assert(!draw(100)); entered=released=false;
 pthread_t worker; assert(!pthread_create(&worker,NULL,delayed_draw,NULL));
 assert(!pthread_mutex_lock(&gate)); while (!entered) assert(!pthread_cond_wait(&cond,&gate));
 assert(!pthread_mutex_unlock(&gate));
 expected_at_phy=2; assert(!draw(2));
 assert(!pthread_mutex_lock(&gate)); released=true; assert(!pthread_cond_broadcast(&cond));
 assert(!pthread_mutex_unlock(&gate)); assert(!pthread_join(worker,NULL));
 assert(get(POWER_SUPPLY_PROP_CURRENT_MAX) == 2000); cleanup(); races++;
 /* Stop is terminal and precedes releasing both controller and PHY. */
 start(); expected_at_phy=0; assert(!draw(500)); mtk_musb_shutdown(&parent);
 unsigned int before=changes; mtk_musb_shutdown(&parent);
 mtk_musb_power_role(&glue.power,USB_ROLE_DEVICE);
 assert(draw(500) == -ESHUTDOWN && changes == before);
 mtk_musb_remove(&parent); assert(child_releases == 1 && phy_releases == 1); cleanup();
 /* Unbind must close the source even when shutdown has never run. */
 start(); expected_at_phy=0; assert(!draw(500));
 mtk_musb_remove(&parent); assert(child_releases == 1 && phy_releases == 1); cleanup();
 /* Init allocation, registration, and managed-action errors unwind in order. */
 setup(); allocation_error=1; assert(mtk_musb_power_init(&parent.dev,&glue.power) == -ENOMEM && !registration_calls); cleanup();
 const int errors[]={-ENOMEM,-EPROBE_DEFER,-EIO};
 for (unsigned int i=0; i<ARRAY_SIZE(errors); i++) {
  setup(); registration_error=errors[i]; assert(mtk_musb_power_init(&parent.dev,&glue.power) == errors[i] && !managed_action); cleanup(); faults++;
 }
 setup(); action_error=-ENOMEM;
 assert(mtk_musb_power_init(&parent.dev,&glue.power) == -ENOMEM && glue.power.stopped);
 cleanup(); faults++;
 printf("PASS: %u budget requests, %u rejected budgets/roles, %u PHY/probe faults and %u supersession interleavings; publication and teardown checked\n",requests,invalid,faults,races);
#else
 setup(); assert(!mtk_musb_power_init(&parent.dev,&glue.power) && !supply_live);
 assert(!draw(UINT_MAX)); phy_error=-EIO; assert(draw(500) == -EIO);
 mtk_musb_shutdown(&parent); mtk_musb_remove(&parent); cleanup();
 puts("PASS: disabled budget option preserves PHY callbacks and errors");
#endif
 /* Unmodified MUSB platforms keep their original PHY path. */
 musb.ops=&legacy_ops; phy_error=0; assert(!draw(UINT_MAX)); phy_error=-EIO; assert(draw(100) == -EIO);
 puts("PASS: other MUSB platforms retain legacy current callback behavior");
 return 0;
}
'''
path = OUT/'host.c'
path.write_text(prelude+body+checks)
for enabled in (1,0):
    target = OUT/f'host-{enabled}'
    subprocess.run(['gcc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
                    '-Wno-unused-parameter','-Wno-unused-function','-Wno-unused-variable',
                    '-pthread','-fsanitize=address,undefined','-fno-omit-frame-pointer','-fno-pie','-no-pie',
                    f'-DBUDGET_ENABLED={enabled}',str(path),'-o',str(target)],check=True)
    subprocess.run([str(target)],check=True,timeout=30)
(ROOT/'out/musb-budget-audit.json').write_text(json.dumps(dict(
    hardware_tested=False, source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    scope='production budget callbacks, power-supply init, MUSB dispatch/role switch/exit, remove and shutdown; modeled PHY, deferred notification and devres; pthread supersession; no electrical draw, timing, detection or charging-policy validation'
),indent=2)+'\n')
