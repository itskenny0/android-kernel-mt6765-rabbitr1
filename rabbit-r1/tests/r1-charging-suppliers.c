// SPDX-License-Identifier: GPL-2.0-only
#include <assert.h>
#include <errno.h>
#include <pthread.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#define EPROBE_DEFER 517
#define GFP_KERNEL 0
#define IS_REACHABLE(x) I2C_REACHABLE
#define READ_ONCE(x) (x)
#define IS_ERR(x) ((uintptr_t)(x) >= (uintptr_t)-4095)
#define ERR_PTR(x) ((void *)(intptr_t)(x))
#define PTR_ERR(x) ((int)(intptr_t)(x))
#define DL_DEV_DRIVER_BOUND 1
#define DL_DEV_UNBINDING 2
#define DL_FLAG_AUTOREMOVE_CONSUMER 4
#define R1_CHARGE_MIN_UA 500000
#define R1_WAITING 0
#define R1_STOPPED 1
struct device_node { int id, refs; };
struct device {
    struct device_node *of_node;
    struct device *parent;
    pthread_mutex_t mutex;
    bool bound, reused, held;
    struct { int status; } links;
    int refs, id;
    void *data;
};
struct platform_device { struct device dev; };
struct i2c_client { struct device dev; };
struct power_supply { struct device dev; int refs; };
struct mutex { bool held; };
struct delayed_work { bool queued; };
struct notifier_block { int (*notifier_call)(struct notifier_block *, unsigned long, void *); };
typedef int atomic_t;
struct r1_charge_policy {
    struct device *dev;
    struct power_supply *charger, *battery, *gadget, *typec;
    struct mutex lock;
    struct delayed_work work;
    struct notifier_block notifier;
    atomic_t generation;
    bool stopping, suspended, cold, hot, full, session, timeout, notifier_registered;
    unsigned long session_start;
    int requested_ua, measured_ua, error;
    struct { int reason; } applied;
};
static struct platform_device consumer, providers[4];
static struct i2c_client client;
static struct power_supply supplies[4];
static struct device_node nodes[5];
static struct r1_charge_policy policy;
static int selected, fail_id, count_value, parse_fail, platform_missing, i2c_missing;
static int link_fail, getter_error, getter_missing, alloc_fail, isolate_fail;
static int self_provider, unbind_after_lookup;
static int action_fail, notifier_fail, group_fail, managed, optional;
static int lookups, gets, locks, unlocks, put_count, isolates, schedules, cancellations;
static int links[5], psyrefs[4], cases, stop_calls, unregs;
static struct device *linked[5];
static void (*stop_action)(void *);
static void *stop_data;
static const char *names[] = {"charger-supply", "battery-supply", "usb-gadget-supply", "typec-supply"};
static void *system_highpri_wq;
static int r1_charge_group;
static void mutex_init(struct mutex *m) { m->held = false; }
static void mutex_lock(struct mutex *m) { assert(!m->held); m->held = true; }
static void mutex_unlock(struct mutex *m) { assert(m->held); m->held = false; }
static void atomic_set(atomic_t *v, int n) { *v = n; }
static void r1_charge_work(void) { }
#define INIT_DELAYED_WORK(w, fn) do { (void)(fn); (w)->queued = false; } while (0)
static int r1_charge_notify(struct notifier_block *nb, unsigned long e, void *d) { return 0; }
static void platform_set_drvdata(struct platform_device *p, void *d) { p->dev.data = d; }
static void *devm_kzalloc(struct device *d, size_t n, int flags) {
    assert(n == sizeof(policy)); if (alloc_fail) return NULL;
    memset(&policy, 0, sizeof(policy)); return &policy;
}
static int dev_err_probe(struct device *d, int ret, const char *msg) { return ret; }
static int name_id(const char *name) { for (int i = 0; i < 4; i++) if (!strcmp(name, names[i])) return i; abort(); }
static int of_count_phandle_with_args(struct device_node *n, const char *name, const char *args) {
    assert(n == &nodes[4] && !args); selected = name_id(name);
    return selected == fail_id ? count_value : 1;
}
static struct device_node *of_parse_phandle(struct device_node *n, const char *name, int index) {
    assert(n == &nodes[4] && !index && selected == name_id(name));
    if (selected == fail_id && parse_fail) return NULL;
    nodes[selected].refs++; return &nodes[selected];
}
static struct platform_device *of_find_device_by_node(struct device_node *n) {
    assert(n == &nodes[selected] && n->refs == 1); lookups++;
    if (selected == fail_id && platform_missing) return NULL;
    if (self_provider) { consumer.dev.refs++; return &consumer; }
    providers[selected].dev.refs++; return &providers[selected];
}
#if I2C_REACHABLE
static struct i2c_client *of_find_i2c_device_by_node(struct device_node *n) {
    assert(n == &nodes[selected] && n->refs == 1);
    if (i2c_missing) return NULL;
    client.dev.refs++; return &client;
}
#endif
static bool dev_of_node_reused(struct device *d) { assert(d->refs >= 2); return d->reused; }
static void of_node_put(struct device_node *n) {
    assert(n->refs == 1); n->refs--;
    if (unbind_after_lookup) {
        assert(providers[selected].dev.refs == 2);
        providers[selected].dev.links.status = DL_DEV_UNBINDING;
        if (unbind_after_lookup == 2) providers[selected].dev.bound = false;
    }
}
static void put_device(struct device *d) { assert(d->refs >= 2 && !d->held); d->refs--; put_count++; }
static bool device_trylock(struct device *d) {
    assert(d->refs >= 2); locks++;
    if (pthread_mutex_trylock(&d->mutex)) return false;
    assert(!d->held); d->held = true; return true;
}
static bool device_is_bound(struct device *d) { assert(d->held && d->refs >= 2); return d->bound; }
static void device_unlock(struct device *d) { assert(d->held && d->refs >= 2); d->held = false; unlocks++; assert(!pthread_mutex_unlock(&d->mutex)); }
static void *device_link_add(struct device *d, struct device *p, int flags) {
    assert(d == &consumer.dev && p->held && p->bound && p->links.status == DL_DEV_DRIVER_BOUND && p->refs >= 2);
    assert(flags == DL_FLAG_AUTOREMOVE_CONSUMER);
    if (selected == fail_id && link_fail) return NULL;
    if (!links[p->id]) { links[p->id] = 1; linked[p->id] = p; p->refs++; }
    return p;
}
static struct power_supply *devm_power_supply_get_by_parent(struct device *d, struct device *p) {
    assert(d == &consumer.dev && p->held && p->refs >= 3 && links[p->id]); gets++;
    if (selected == fail_id && getter_error) return ERR_PTR(getter_error);
    if (selected == fail_id && getter_missing) return NULL;
    assert(supplies[selected].dev.parent == p);
    supplies[selected].refs++; psyrefs[selected]++; return &supplies[selected];
}
static int r1_charge_isolate(struct r1_charge_policy *p) {
    assert(p->charger == &supplies[0] && links[0] && supplies[0].refs > 1); isolates++;
    if (p->stopping) stop_calls++;
    return isolate_fail ? -EIO : 0;
}
static bool device_property_read_bool(struct device *d, const char *name) {
    assert(d == &providers[0].dev && links[0] && !strcmp(name, "richtek,managed-charging")); return managed;
}
static bool device_property_present(struct device *d, const char *name) {
    assert(d == &consumer.dev && !strcmp(name, "typec-supply")); return optional;
}
static int devm_add_action_or_reset(struct device *d, void (*fn)(void *), void *data) {
    if (action_fail) { fn(data); return -ENOMEM; } stop_action = fn; stop_data = data; return 0;
}
static int power_supply_reg_notifier(struct notifier_block *nb) { return notifier_fail ? -EIO : 0; }
static void power_supply_unreg_notifier(struct notifier_block *nb) { assert(policy.stopping); unregs++; }
static int devm_device_add_group(struct device *d, void *group) { assert(group == &r1_charge_group); return group_fail ? -EIO : 0; }
static void mod_delayed_work(void *q, struct delayed_work *w, unsigned long delay) { assert(!delay); w->queued = true; schedules++; }
static void cancel_delayed_work_sync(struct delayed_work *w) { w->queued = false; cancellations++; }
#include "under-test.h"
static void cleanup(void) {
    if (stop_action) { stop_action(stop_data); stop_action = NULL; }
    for (int i = 0; i < 4; i++) { supplies[i].refs -= psyrefs[i]; psyrefs[i] = 0; assert(supplies[i].refs == 1); }
    // Models driver-core probe failure/unbind link removal after devres cleanup.
    for (int i = 0; i < 5; i++) if (links[i]) { linked[i]->refs--; links[i] = 0; }
    for (int i = 0; i < 4; i++) { assert(providers[i].dev.refs == 1 && !providers[i].dev.held && !nodes[i].refs); }
    assert(client.dev.refs == 1 && !client.dev.held);
}
static void fresh(void) {
    cleanup(); cases++;
    selected = 0; fail_id = -1; count_value = 1;
    parse_fail = platform_missing = link_fail = getter_error = getter_missing = alloc_fail = isolate_fail = 0;
    self_provider = unbind_after_lookup = 0;
    action_fail = notifier_fail = group_fail = optional = 0; managed = i2c_missing = 1;
    lookups = gets = locks = unlocks = put_count = isolates = schedules = cancellations = stop_calls = unregs = 0;
    for (int i = 0; i < 4; i++) { providers[i].dev.bound = true; providers[i].dev.reused = false; providers[i].dev.links.status = DL_DEV_DRIVER_BOUND; supplies[i].dev.parent = &providers[i].dev; }
    client.dev.bound = true; client.dev.links.status = DL_DEV_DRIVER_BOUND;
}
static int supply(int id, int expected) {
    struct power_supply *out = (void *)0x1000;
    int ret = r1_charge_supply(&consumer.dev, names[id], &out);
    assert(ret == expected);
    assert(out == (ret ? (void *)0x1000 : &supplies[id])); return ret;
}
static pthread_barrier_t entered, release_lock;
static void *hold_provider(void *data) {
    struct device *d = data; assert(!pthread_mutex_lock(&d->mutex));
    pthread_barrier_wait(&entered); pthread_barrier_wait(&release_lock);
    assert(!pthread_mutex_unlock(&d->mutex)); return NULL;
}
int main(void) {
    consumer.dev.of_node = &nodes[4]; consumer.dev.refs = 1;
    for (int i = 0; i < 4; i++) { providers[i].dev.refs = 1; providers[i].dev.id = i; assert(!pthread_mutex_init(&providers[i].dev.mutex, NULL)); supplies[i].refs = 1; }
    client.dev.refs = 1; client.dev.id = 4; assert(!pthread_mutex_init(&client.dev.mutex, NULL));
    for (int id = 0; id < 4; id++) {
        fresh(); supply(id, 0); assert(locks == 1 && unlocks == 1 && put_count == 1 && gets == 1);
        fresh(); fail_id = id; count_value = -ENOENT; supply(id, -ENOENT); assert(!lookups && !gets);
        fresh(); fail_id = id; count_value = 0; supply(id, -EINVAL);
        fresh(); fail_id = id; count_value = 2; supply(id, -EINVAL);
        fresh(); fail_id = id; parse_fail = 1; supply(id, -EINVAL); assert(!lookups);
        fresh(); fail_id = id; platform_missing = 1; supply(id, -EPROBE_DEFER); assert(!locks);
        fresh(); providers[id].dev.reused = true; supply(id, -EPROBE_DEFER); assert(!locks && put_count == 1);
        fresh(); providers[id].dev.bound = false; supply(id, -EPROBE_DEFER); assert(!gets && unlocks == 1);
        fresh(); providers[id].dev.links.status = DL_DEV_UNBINDING; supply(id, -EPROBE_DEFER); assert(!gets && unlocks == 1);
        fresh(); providers[id].dev.links.status = 0; supply(id, -EPROBE_DEFER); assert(!gets);
        fresh(); fail_id = id; link_fail = 1; supply(id, -ENOMEM); assert(!gets && unlocks == 1);
        fresh(); fail_id = id; getter_missing = 1; supply(id, -EPROBE_DEFER); assert(gets == 1);
        fresh(); fail_id = id; getter_error = -ENOMEM; supply(id, -ENOMEM);
        fresh(); fail_id = id; getter_error = -ENODEV; supply(id, -ENODEV);
        fresh(); assert(!pthread_barrier_init(&entered, NULL, 2)); assert(!pthread_barrier_init(&release_lock, NULL, 2));
        pthread_t thread; assert(!pthread_create(&thread, NULL, hold_provider, &providers[id].dev));
        pthread_barrier_wait(&entered); supply(id, -EPROBE_DEFER); assert(!gets && !unlocks && put_count == 1);
        pthread_barrier_wait(&release_lock); assert(!pthread_join(thread, NULL));
        assert(!pthread_barrier_destroy(&entered)); assert(!pthread_barrier_destroy(&release_lock));
    }
    fresh(); fail_id = 3; platform_missing = 1; i2c_missing = 0; supplies[3].dev.parent = &client.dev;
    supply(3, I2C_REACHABLE ? 0 : -EPROBE_DEFER);
    fresh(); self_provider = 1; supply(0, -EINVAL); assert(!locks && consumer.dev.refs == 1);
    fresh(); unbind_after_lookup = 1; supply(0, -EPROBE_DEFER); assert(!gets && unlocks == 1);
    fresh(); unbind_after_lookup = 2; supply(0, -EPROBE_DEFER); assert(!gets && unlocks == 1);
    fresh(); fail_id = 3; platform_missing = 1; i2c_missing = 0; client.dev.bound = false;
    supply(3, -EPROBE_DEFER); assert(!gets);
    fresh(); fail_id = 3; platform_missing = 1; i2c_missing = 0; client.dev.links.status = DL_DEV_UNBINDING;
    supply(3, -EPROBE_DEFER); assert(!gets);
    fresh(); supply(0, 0); supply(0, 0); assert(links[0] == 1 && supplies[0].refs == 3 && providers[0].dev.refs == 2);
    fresh(); assert(!r1_charge_probe(&consumer)); assert(policy.requested_ua == 500000 && !policy.typec && gets == 3 && isolates == 1 && schedules == 1); cleanup(); assert(stop_calls == 1 && unregs == 1 && cancellations == 1);
    fresh(); optional = 1; assert(!r1_charge_probe(&consumer)); assert(policy.typec == &supplies[3] && gets == 4); cleanup(); assert(stop_calls == 1);
    fresh(); alloc_fail = 1; assert(r1_charge_probe(&consumer) == -ENOMEM && !lookups);
    fresh(); isolate_fail = 1; assert(r1_charge_probe(&consumer) == -EIO && gets == 1 && isolates == 1 && !schedules);
    fresh(); managed = 0; assert(r1_charge_probe(&consumer) == -EINVAL && gets == 1 && isolates == 1);
    for (int id = 0; id < 4; id++) { fresh(); optional = 1; fail_id = id; getter_missing = 1; assert(r1_charge_probe(&consumer) == -EPROBE_DEFER); assert(isolates == (id ? 1 : 0) && !schedules); }
    fresh(); action_fail = 1; assert(r1_charge_probe(&consumer) == -ENOMEM && stop_calls == 1 && !schedules);
    fresh(); notifier_fail = 1; assert(r1_charge_probe(&consumer) == -EIO); cleanup(); assert(stop_calls == 1 && !unregs);
    fresh(); group_fail = 1; assert(r1_charge_probe(&consumer) == -EIO); cleanup(); assert(stop_calls == 1 && unregs == 1 && !schedules);
    cleanup();
    for (int i = 0; i < 4; i++) assert(!pthread_mutex_destroy(&providers[i].dev.mutex));
    assert(!pthread_mutex_destroy(&client.dev.mutex));
    printf("%d supplier/probe/stop scenarios pass (I2C reachable=%d); references, locks, output publication and link-before-get asserted; four real threaded busy locks rejected\n", cases, I2C_REACHABLE);
    return 0;
}
