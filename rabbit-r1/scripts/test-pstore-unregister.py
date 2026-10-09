#!/usr/bin/env python3
"""Exercise production pstore teardown and cleaner with a modeled workqueue."""
from pathlib import Path
import argparse,hashlib,json,os,resource,subprocess
ROOT=Path('/rabbitr1')

def extract(source,signature):
 a=source.index(signature);b=source.index('\n}',a)+2
 return source[a:b]+'\n'

PRE=r'''
#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdio.h>
struct work_struct { int unused; };
struct delayed_work { int unused; };
struct pstore_zone { int unused; };
struct pstore_zone_info { int unused; };
struct psi { void *buf; int bufsize; };
struct psz_context {
 int pstore_zone_info_lock, kmsg_max_cnt, ftrace_max_cnt;
 struct pstore_zone_info *pstore_zone_info;
 struct pstore_zone *ppsz, *cpsz, **kpszs, **fpszs;
 struct psi pstore;
 int oops_counter, panic_counter, recovered, on_panic;
};
static struct psz_context pstore_zone_cxt;
static struct delayed_work psz_cleaner;
static bool queued, running, callbacks, fail_writes;
static int writes, cancellations, frees, checked;
static void psz_flush_all_dirty_zones(struct work_struct *);
static void mutex_lock(int *p) { (void)p; }
static void mutex_unlock(int *p) { (void)p; }
static void atomic_set(int *p,int v) { *p=v; }
static int msecs_to_jiffies(int n) { return n; }
static void schedule_delayed_work(struct delayed_work *w,int delay)
{ assert(w==&psz_cleaner && delay==1000); queued=true; }
static int psz_flush_dirty_zone(struct pstore_zone *z)
{
 assert(z && pstore_zone_cxt.pstore_zone_info && !running);
 writes++; return fail_writes ? -5 : 0;
}
static int psz_flush_dirty_zones(struct pstore_zone **z,unsigned n)
{ assert(z && n); return psz_flush_dirty_zone(z[0]); }
static void pstore_unregister(struct psi *p)
{ assert(p==&pstore_zone_cxt.pstore && callbacks); callbacks=false; }
static void cancel_delayed_work_sync(struct delayed_work *w)
{
 assert(w==&psz_cleaner); cancellations++;
 if(running) { running=false; psz_flush_all_dirty_zones(NULL); }
 queued=false;
}
/* Old flush waits one instance. Its callback can leave a new retry queued. */
static void __attribute__((unused)) flush_delayed_work(struct delayed_work *w)
{
 assert(w==&psz_cleaner);
 if(queued || running) { queued=false; running=false; psz_flush_all_dirty_zones(NULL); }
}
static void kfree(void *p)
{ (void)p; assert(!queued && !running && !callbacks); frees++; }
static void psz_free_all_zones(struct psz_context *c)
{ assert(c==&pstore_zone_cxt && !queued && !running && !callbacks); }
'''
POST=r'''
int main(void) {
 struct pstore_zone_info info;
 struct pstore_zone zone;
 for(int pending=0;pending<2;pending++)
  for(int active=0;active<2;active++)
   for(int fault=0;fault<2;fault++) {
    pstore_zone_cxt=(struct psz_context){.pstore_zone_info=&info,.cpsz=&zone,
     .pstore={.buf=&zone,.bufsize=1},.oops_counter=4,.panic_counter=5,.recovered=1};
    queued=pending;running=active;callbacks=true;fail_writes=fault;
    writes=cancellations=frees=0;
    unregister_pstore_zone(&info);
    assert(!queued && !running && !callbacks && frees==1 && writes>=1);
    assert(!pstore_zone_cxt.pstore_zone_info && !pstore_zone_cxt.pstore.buf);
    assert(!pstore_zone_cxt.recovered && !pstore_zone_cxt.oops_counter);
    checked++;
   }
 pstore_zone_cxt.pstore_zone_info=NULL;writes=cancellations=frees=0;
 unregister_pstore_zone(&info);assert(!writes && !cancellations && !frees);checked++;
 printf("%d teardown scenarios passed\n",checked);return 0;
}
'''
def main():
 ap=argparse.ArgumentParser();ap.add_argument('--out',type=Path,required=True);a=ap.parse_args()
 out=a.out.resolve();assert out.is_relative_to(ROOT);out.mkdir(parents=True,exist_ok=False)
 resource.setrlimit(resource.RLIMIT_CORE,(0,0))
 source=ROOT/'src/mainline/fs/pstore/zone.c';text=source.read_text()
 worker=extract(text,'static void psz_flush_all_dirty_zones(struct work_struct *work)\n{')
 body=extract(text,'void unregister_pstore_zone(struct pstore_zone_info *info)\n{')
 token='\tcancel_delayed_work_sync(&psz_cleaner);';assert body.count(token)==2
 variants={'production':body,'missing-first-cancel':body.replace(token,'',1),
  'missing-final-cancel':body[:body.rindex(token)]+body[body.rindex(token)+len(token):],
  'previous-teardown':body.replace(token+'\n','',1).replace(token,'\tflush_delayed_work(&psz_cleaner);')}
 rows=[];env=dict(os.environ,TMPDIR=str(ROOT/'.tmp'),ASAN_OPTIONS='detect_leaks=1:abort_on_error=1')
 for name,code in variants.items():
  c=out/(name+'.c');exe=out/name;c.write_text(PRE+worker+code+POST)
  subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-function','-Wno-unused-parameter','-fsanitize=address,undefined','-fno-omit-frame-pointer','-g','-no-pie',str(c),'-o',str(exe)],check=True,env=env)
  r=subprocess.run([str(exe)],capture_output=True,text=True,env=env,timeout=5)
  (out/(name+'.stdout')).write_text(r.stdout);(out/(name+'.stderr')).write_text(r.stderr)
  assert (r.returncode==0)==(name=='production'),(name,r.stdout,r.stderr)
  rows.append({'case':name,'exit_code':r.returncode,'expected':True})
 result={'passed':True,'scenarios':9,'rejected_regressions':3,'rows':rows,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),'scope':'Production teardown/cleaner with modeled workqueue, I/O and callback lifetime; ASan/UBSan. No storage durability or physical poweroff claim.'}
 (out/'result.json').write_text(json.dumps(result,indent=2)+'\n');print(json.dumps(result))
if __name__=='__main__':main()
