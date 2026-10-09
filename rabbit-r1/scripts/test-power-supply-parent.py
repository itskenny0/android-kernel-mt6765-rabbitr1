#!/usr/bin/env python3
"""Exercise extracted parent getter, class_find_device and put callbacks.
Class iteration/devres/atomic/device references are explicit host models;
no concurrent kernel or hardware behavior is claimed.
"""
from pathlib import Path
import argparse,hashlib,json,os,shutil,subprocess
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--kernel-root',type=Path,default=Path('/rabbitr1/src/mainline'))
parser.add_argument('--output-dir',type=Path,default=Path('/rabbitr1/out/power-supply-parent'))
parser.add_argument('--cc',default='cc')
args=parser.parse_args()
ROOT=args.kernel_root.resolve()
BASE=args.output_dir.resolve()
if not ROOT.is_relative_to('/rabbitr1') or not BASE.is_relative_to('/rabbitr1'):
    parser.error('source and output must stay under /rabbitr1')
BASE.mkdir(parents=True,exist_ok=True)
os.environ['TMPDIR']='/rabbitr1/.tmp'
compiler=shutil.which(args.cc)
if not compiler: parser.error('compiler not found: '+args.cc)
def extract(path,signature):
    text=path.read_text(); start=text.index(signature); brace=text.index('{',start); depth=1; end=brace+1
    while depth:
        depth+=(text[end]=='{')-(text[end]=='}');end+=1
    return text[start:end]
core=ROOT/'drivers/power/supply/power_supply_core.c'; cls=ROOT/'drivers/base/class.c'
functions=[(cls,'struct device *class_find_device('),(core,'void power_supply_put('),(core,'static void devm_power_supply_put('),(core,'static int power_supply_match_parent('),(core,'struct power_supply *devm_power_supply_get_by_parent(')]
source_hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in {p for p,_ in functions}}
parts=[extract(p,s) for p,s in functions]
prefix=r'''
#define _GNU_SOURCE
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <sys/mman.h>
#include <unistd.h>
struct resource;
struct device { struct device *parent; int refs; struct resource *resources; };
struct power_supply { struct device dev; int use_cnt; };
struct class { const char *name; };
static const struct class power_supply_class={.name="power_supply"};
struct subsys_private { int refs; };
static struct subsys_private subsystem;
struct class_dev_iter { size_t next; struct device *held; };
typedef int (*device_match_t)(struct device *,const void *);
static struct device *devices[8]; static size_t device_count;
static int alloc_fail, allocations, frees, find_calls, callbacks, case_count;
#define GFP_KERNEL 0
#define ERR_PTR(x) ((void *)(intptr_t)(x))
#define dev_to_psy(x) ((struct power_supply *)((char *)(x)-offsetof(struct power_supply,dev)))
#define WARN(condition,...) assert(!(condition))
#define atomic_inc(x) (++*(x))
#define atomic_dec(x) (--*(x))
static struct device *get_device(struct device *dev) { assert(dev && dev->refs>0);dev->refs++;return dev; }
static void put_device(struct device *dev) { assert(dev && dev->refs>0);dev->refs--; }
static struct subsys_private *class_to_subsys(const struct class *c) { assert(c==&power_supply_class);subsystem.refs++;return &subsystem; }
static void subsys_put(struct subsys_private *sp) { assert(sp==&subsystem && sp->refs>0);sp->refs--; }
static void class_dev_iter_init(struct class_dev_iter *iter,const struct class *c,const struct device *start,void *type) {
    assert(c==&power_supply_class && !start && !type);iter->next=0;iter->held=NULL;find_calls++;
}
static struct device *class_dev_iter_next(struct class_dev_iter *iter) {
    if (iter->held) put_device(iter->held);
    iter->held=iter->next<device_count?get_device(devices[iter->next++]):NULL;
    return iter->held;
}
static void class_dev_iter_exit(struct class_dev_iter *iter) { if(iter->held)put_device(iter->held);iter->held=NULL; }
struct resource { struct resource *next; void (*release)(struct device *,void *); void *payload; };
static void *devres_alloc(void (*release)(struct device *,void *),size_t n,int flags) {
    assert(n==sizeof(struct power_supply *) && flags==GFP_KERNEL);
    if(alloc_fail)return NULL;
    struct resource *r=calloc(1,sizeof(*r));assert(r);r->release=release;allocations++;return &r->payload;
}
static struct resource *res_from(void *ptr) { return (struct resource *)((char *)ptr - offsetof(struct resource,payload)); }
static void devres_free(void *ptr) { assert(ptr);free(res_from(ptr));frees++; }
static void devres_add(struct device *dev,void *ptr) { assert(dev);struct resource *r=res_from(ptr);r->next=dev->resources;dev->resources=r; }
static void cleanup(struct device *dev) {
    while(dev->resources) {struct resource *r=dev->resources;dev->resources=r->next;r->release(dev,&r->payload);callbacks++;free(r);frees++;}
}
'''
main=r'''
static void fresh(void) {assert(allocations==frees);assert(subsystem.refs==0);device_count=0;alloc_fail=0;find_calls=0;case_count++;}
int main(void) {
    struct device consumer={.refs=1}, provider={.refs=1}, other={.refs=1};
    struct power_supply chosen={.dev={.parent=&provider,.refs=1},.use_cnt=1};
    struct power_supply second={.dev={.parent=&provider,.refs=1},.use_cnt=1};
    struct power_supply unrelated={.dev={.parent=&other,.refs=1},.use_cnt=1};
    fresh();assert(devm_power_supply_get_by_parent(&consumer,NULL)==ERR_PTR(-EINVAL));assert(!find_calls);
    fresh();alloc_fail=1;assert(devm_power_supply_get_by_parent(&consumer,&provider)==ERR_PTR(-ENOMEM));assert(!find_calls);
    fresh();assert(devm_power_supply_get_by_parent(&consumer,&provider)==NULL);assert(find_calls==1);assert(!consumer.resources);
    fresh();devices[device_count++]=&unrelated.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==NULL);assert(unrelated.dev.refs==1 && unrelated.use_cnt==1);
    fresh();devices[device_count++]=&chosen.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==&chosen);assert(chosen.dev.refs==2 && chosen.use_cnt==2);assert(provider.refs==1);cleanup(&consumer);assert(chosen.dev.refs==1 && chosen.use_cnt==1);
    fresh();devices[device_count++]=&unrelated.dev;devices[device_count++]=&chosen.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==&chosen);assert(unrelated.dev.refs==1 && unrelated.use_cnt==1);cleanup(&consumer);
    fresh();devices[device_count++]=&chosen.dev;devices[device_count++]=&second.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==&chosen);assert(second.dev.refs==1 && second.use_cnt==1);cleanup(&consumer);
    fresh();devices[device_count++]=&chosen.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==&chosen);assert(devm_power_supply_get_by_parent(&consumer,&provider)==&chosen);assert(chosen.dev.refs==3 && chosen.use_cnt==3);cleanup(&consumer);assert(chosen.dev.refs==1 && chosen.use_cnt==1);
    long page=sysconf(_SC_PAGESIZE);assert(page>0);void *dead=mmap(NULL,(size_t)page,PROT_NONE,MAP_PRIVATE|MAP_ANONYMOUS,-1,0);assert(dead!=MAP_FAILED);
    unrelated.dev.parent=dead;
    fresh();devices[device_count++]=&unrelated.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==NULL);assert(unrelated.dev.refs==1 && unrelated.use_cnt==1);
    fresh();devices[device_count++]=&unrelated.dev;devices[device_count++]=&chosen.dev;assert(devm_power_supply_get_by_parent(&consumer,&provider)==&chosen);assert(unrelated.dev.refs==1 && unrelated.use_cnt==1);cleanup(&consumer);
    assert(munmap(dead,(size_t)page)==0);
    assert(allocations==frees && !consumer.resources && !subsystem.refs);
    assert(chosen.dev.refs==1 && chosen.use_cnt==1 && second.dev.refs==1 && unrelated.dev.refs==1);
    printf("%d helper scenarios pass; allocations=%d frees=%d managed_put_callbacks=%d; all child/use_cnt/provider references balanced\n",case_count,allocations,frees,callbacks);
    return 0;
}
'''
source=prefix+'\n\n'.join(parts)+main
path=BASE/'parent-helper-test.c';path.write_text(source)
cmd=[compiler,'-std=c11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-O1','-g','-fsanitize=address,undefined','-fno-sanitize-recover=all','-fno-omit-frame-pointer','-fno-pie','-no-pie',str(path),'-o',str(BASE/'parent-helper-test')]
compiled=subprocess.run(cmd,text=True,capture_output=True);(BASE/'helper-compile.log').write_text(compiled.stdout+compiled.stderr);compiled.check_returncode()
run=subprocess.run([str(BASE/'parent-helper-test')],text=True,capture_output=True);(BASE/'helper-run.log').write_text(run.stdout+run.stderr);run.check_returncode()
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
assert all(sha(Path(p))==value for p,value in source_hashes.items()), "sources changed during helper test"
record={'scope':__doc__,'result':'pass','sanitizers':['address','undefined'],'compile_command':cmd,'output':run.stdout.strip(),'source_files':{str(p):sha(p) for p in [core,cls,ROOT/'drivers/power/supply/mt6357-gauge.c',ROOT/'include/linux/power_supply.h']},'functions':[{'signature':sig,'path':str(p),'sha256':hashlib.sha256(body.encode()).hexdigest()} for (p,sig),body in zip(functions,parts)],'modeled_services':['class iterator child references','device get/put counters','devres allocation and reverse-order release','atomic use_cnt increment/decrement'],'limitations':['No concurrent in-kernel race execution','No supplier device-link state-machine execution in this fixture','No physical charger access'],'artifacts':{p.name:sha(p) for p in [Path(__file__),path,BASE/'parent-helper-test',BASE/'helper-compile.log',BASE/'helper-run.log']}}
(BASE/'helper-result.json').write_text(json.dumps(record,indent=2)+'\n');print(run.stdout.strip())
