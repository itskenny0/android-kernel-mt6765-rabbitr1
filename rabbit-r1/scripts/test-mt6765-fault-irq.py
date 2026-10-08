#!/usr/bin/env python3
"""Check native IOMMU fault classification, snapshots and global acknowledgement.

Production handlers execute under ASan/UBSan. Status latches, MMIO, callbacks
and TLB invalidation are models; this is not an interrupt or DMA hardware test.
"""
import argparse
import importlib.util
from pathlib import Path
import subprocess

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
spec = importlib.util.spec_from_file_location('fault', Path(__file__).with_name('test-mt6765-fault.py'))
fault = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fault)

MODEL = r'''
#include <stdarg.h>
#define FIELD_GET(mask,value) (((value)&(mask))>>__builtin_ctzl(mask))
#define IRQ_NONE 0
#define IRQ_HANDLED 1
#define IOMMU_FAULT_READ 0
#define IOMMU_FAULT_WRITE 1
#define str_write_read(x) ((x)?"write":"read")
struct mtk_iommu_data { const struct mtk_iommu_plat_data *plat_data;struct mtk_iommu_bank_data *bank; };
static u32 regs[0x1000/4];
static struct mtk_iommu_bank_data *current;
static unsigned int reports,diagnostics,l2_logs,main_logs,unknown_logs,acks,flushes,writes;
static unsigned int reads[0x1000/4];
static u32 main_status,l2_status,walk_status,error_status[2],unknown_status,logged_l2,logged_walk;
static u64 addresses[4];
static bool directions[4],locked,inject_late,clear_pending;
static int disposition;
static u32 raw_va[2]={0x12345a83,0x6789a001};
static u32 raw_pa[2]={0x10203040,0xfedcba98};
static u32 raw_id[2]={(1<<7)|(7<<2)|3,(3<<7)|(20<<2)};
static u32 readl_relaxed(const void *p) {
    unsigned int off=(const u32 *)p-regs;assert(off<ARRAY_SIZE(regs));reads[off]++;
    if(off==0x120/4 && clear_pending){
        clear_pending=false;acks++;
        regs[0x130/4]=regs[0x134/4]=0;
        for(unsigned int i=0;i<2;i++){
            regs[(0x13c+8*i)/4]=regs[(0x140+8*i)/4]=regs[(0x150+4*i)/4]=0xdead0000;
        }
        regs[0x138/4]=0xdead0001;
    }
    return regs[off];
}
#define readl readl_relaxed
static void writel_relaxed(u32 value,void *p) {
    unsigned int off=(u32 *)p-regs;assert(off<ARRAY_SIZE(regs));writes++;
    if(off==0x120/4){
        assert(value==(regs[off]|0x1000) && !clear_pending);
        /* Defer the posted global clear until the driver reads it back. */
        clear_pending=true;
    }else assert(off==0x38/4 || off==0x20/4);
    regs[off]=value;
}
#define spin_lock_irqsave(lock,flags) do { assert(!locked);locked=true;(void)(lock);(flags)=0; } while(0)
#define spin_unlock_irqrestore(lock,flags) do { assert(locked);locked=false;(void)(lock);(void)(flags); } while(0)
static void wmb(void) { assert(locked && regs[0x38/4]==3 && regs[0x20/4]==2);flushes++; }
static int report_iommu_fault(struct iommu_domain *domain,struct device *dev,u64 address,int write) {
    assert(current->m4u_dom && domain==&current->m4u_dom->domain && dev==current->parent_dev);
    assert(main_status&0x81);
    assert(acks==1 && reports<ARRAY_SIZE(addresses));
    addresses[reports]=address;directions[reports++]=write;
    if(inject_late){
        inject_late=false;regs[0x134/4]=1;regs[0x13c/4]=0xbad00002;
        regs[0x140/4]=0x66778899;regs[0x150/4]=4;
        /* The second old fault must already have been captured. */
        regs[0x144/4]=0x99999002;regs[0x148/4]=0x55555555;regs[0x154/4]=0;
    }
    return disposition;
}
static void dev_err_ratelimited(struct device *dev,const char *fmt,...) {
    assert(dev==current->parent_dev);va_list args;va_start(args,fmt);
    if(!strncmp(fmt,"fault type=",11)){
        u32 status=va_arg(args,u32);u64 va=va_arg(args,u64),pa=va_arg(args,u64);
        u32 id=va_arg(args,u32);unsigned int larb=va_arg(args,unsigned int),port=va_arg(args,unsigned int);
        bool layer=va_arg(args,int);bool write=!strcmp(va_arg(args,const char *),"write");
        unsigned int i=id==raw_id[0]?0:1;
        assert(status==main_status && va==(raw_va[i]&0xfffff000) && pa==raw_pa[i] && id==raw_id[i]);
        assert(larb==((id>>7)&7) && port==((id>>2)&31) && layer==!!(raw_va[i]&1) && write==!!(raw_va[i]&2));
        diagnostics++;
    }else if(!strcmp(fmt,"L2 fault status=0x%x table-walk VA register=0x%x\n")){
        logged_l2=va_arg(args,u32);logged_walk=va_arg(args,u32);l2_logs++;
    }else if(!strcmp(fmt,"L2 fault status=0x%x\n")){
        logged_l2=va_arg(args,u32);l2_logs++;
    }else if(!strcmp(fmt,"MMU%u non-translation fault status=0x%x\n")){
        unsigned int i=va_arg(args,unsigned int);assert(i<2);error_status[i]=va_arg(args,u32);main_logs++;
    }else if(!strcmp(fmt,"Unexpected main fault status=0x%x\n")){
        unknown_status=va_arg(args,u32);unknown_logs++;
    }else assert(!"unexpected diagnostic");
    va_end(args);
}
'''

TESTS = r'''
static void fault_case(u32 main,u32 l2,int result,bool late) {
    struct device dev={0};struct mtk_iommu_domain dom={0};
    struct mtk_iommu_bank_data bank={.base=regs,.parent_dev=&dev,.irq=73};
    struct mtk_iommu_data data={.plat_data=&mt6765_data,.bank=&bank};
    bank.parent_data=&data;bank.m4u_dom=result<0?NULL:&dom;current=&bank;
    memset(regs,0,sizeof(regs));memset(reads,0,sizeof(reads));memset(error_status,0,sizeof(error_status));
    reports=diagnostics=l2_logs=main_logs=unknown_logs=acks=flushes=writes=0;
    unknown_status=logged_l2=logged_walk=0;inject_late=late;clear_pending=false;disposition=result;
    main_status=main;l2_status=l2;walk_status=0x98765ab1;
    regs[0x120/4]=0xa55a006f;regs[0x130/4]=l2;regs[0x134/4]=main;regs[0x138/4]=walk_status;
    for(unsigned int i=0;i<2;i++){
        regs[(0x13c+8*i)/4]=raw_va[i];regs[(0x140+8*i)/4]=raw_pa[i];regs[(0x150+4*i)/4]=raw_id[i];
    }
    assert(mtk_iommu_isr(73,&bank)==((main||l2)?IRQ_HANDLED:IRQ_NONE));
    unsigned int translations=!!(main&1)+!!(main&0x80);
    assert(reports==(result<0?0:translations));
    assert(diagnostics==(result==0?0:translations));
    unsigned int at=0;
    for(unsigned int i=0;i<2;i++){
        bool translation=main&(1U<<(7*i));
        assert(reads[(0x13c+8*i)/4]==translation && reads[(0x140+8*i)/4]==translation && reads[(0x150+4*i)/4]==translation);
        if(translation && result>=0){assert(addresses[at]==(raw_va[i]&0xfffff000) && directions[at]==!!(raw_va[i]&2));at++;}
        assert(error_status[i]==((main>>(7*i))&0x7e));
    }
    assert(main_logs==!!(main&0x7e)+!!(main&0x3f00));
    assert(unknown_status==(main&~0x3fffU) && unknown_logs==!!unknown_status);
    assert(l2_logs==!!l2 && logged_l2==l2);
    assert(reads[0x138/4]==!!(l2&2) && logged_walk==((l2&2)?walk_status:0));
    assert(acks==!!(main||l2) && flushes==acks && writes==3*acks && !locked && !clear_pending);
    assert(regs[0x130/4]==0 && regs[0x134/4]==(late?1U:0U));
    if(late){
        /* Service the new pending fault on the next invocation. */
        assert(!result);main_status=1;acks=reports=flushes=writes=0;
        memset(reads,0,sizeof(reads));
        assert(mtk_iommu_isr(73,&bank)==IRQ_HANDLED);
        assert(reports==1 && addresses[0]==0xbad00000 && directions[0]);
        assert(acks==1 && flushes==1 && writes==3 && !regs[0x134/4]);
        assert(!reads[0x144/4] && !reads[0x148/4] && !reads[0x154/4]);
    }
}
int main(void) {
    unsigned int cases=0;
    /* Every combination of the fourteen main fault bits, including both slaves. */
    for(unsigned int main=0;main<0x4000;main++) { fault_case(main,0,0,false);cases++; }
    /* Every documented L2 status combination against zero, one or two translations. */
    const unsigned int main_masks[]={0,1,0x80,0x81};
    for(unsigned int l2=0;l2<512;l2++)for(unsigned int m=0;m<4;m++)for(int result=-1;result<=1;result++){
        fault_case(main_masks[m],l2,result,false);cases++;
    }
    for(unsigned int bit=14;bit<32;bit++){fault_case(1U<<bit,0,0,false);cases++;}
    fault_case(0x81,0x1ff,0,true);cases++;
    puts("MMIO/status latches, fault callbacks and TLB completion are models; hardware routing and fault recovery are untested.");
    printf("PASS: %u native IOMMU IRQ cases; L2-only, simultaneous slaves, non-translation errors, snapshots and late faults\n",cases);
}
'''


def build(source):
    s = source.read_text()
    code = fault.setup.PRELUDE+s[s.index('#define REG_MMU_PT_BASE_ADDR'):s.index('enum mtk_iommu_plat')]
    for name in ('mtk_iommu_plat','mtk_iommu_iova_region','mtk_iommu_plat_data',
                 'mtk_iommu_bank_data','single_domain','mt6765_data'):
        code += fault.smi.block(s,name)
    code += MODEL
    for name in ('mtk_iommu_tlb_flush_all','mt6765_iommu_isr','mtk_iommu_isr'):
        if name != 'mt6765_iommu_isr' or 'static irqreturn_t mt6765_iommu_isr(' in s:
            code += fault.smi.block(s,name)
    directory = ROOT/'out/mt6765-fault-irq'
    directory.mkdir(exist_ok=True)
    generated = directory/'fault.c';generated.write_text(code+TESTS)
    binary = generated.with_suffix('')
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
        '-Wno-sign-compare','-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g',
        '-I'+str(SRC/'include'),str(generated),'-o',str(binary)],check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=SRC/'drivers/iommu/mtk_iommu.c')
    args = parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT): raise SystemExit('Source must remain under /rabbitr1')
    subprocess.run([str(build(args.source))],check=True,timeout=30)


if __name__ == '__main__': main()
