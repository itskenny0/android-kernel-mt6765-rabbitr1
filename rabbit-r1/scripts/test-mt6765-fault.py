#!/usr/bin/env python3
"""Audit MT6765 fault reports against the shipped handler."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import resource
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import *

ROOT = Path('/rabbitr1')
SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
spec = importlib.util.spec_from_file_location('smi', Path(__file__).with_name('test-mt6765-smi.py'))
smi = importlib.util.module_from_spec(spec); spec.loader.exec_module(smi)

spec = importlib.util.spec_from_file_location('setup', Path(__file__).with_name('test-mt6765-iommu.py'))
setup = importlib.util.module_from_spec(spec); spec.loader.exec_module(setup)

def stock_faults(raw):
    bias,obj,mmio,stop=0x1000000,0x20000000,0x30000000,0x40000000
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    uc.mem_map(bias,0x2000000);uc.mem_write(bias,raw)
    uc.mem_map(obj,0x10000);uc.mem_map(mmio,0x5000);uc.mem_map(stop,0x1000)
    uc.mem_write(bias+0x1aa4c70,struct.pack('<Q',obj))
    uc.mem_write(obj+112,struct.pack('<I',73))
    uc.mem_write(bias+0x1aa4740,struct.pack('<Q',mmio))
    uc.mem_write(bias+0x1aa4750,struct.pack('<4Q',*(mmio+0x1000*(i+1) for i in range(4))))
    ports=[]
    for i in range(52):
        fields=struct.unpack_from('<I',raw,0x179ddb0+i*48+8)[0]
        ports.append(dict(index=i,larb=(fields>>4)&15,port=(fields>>8)&255,tfid=(fields>>16)&0xfff))
        uc.mem_write(bias+0x179ddb0+i*48+12,b'\0') # diagnostic callback disabled
    saved=[UC_ARM64_REG_X19,UC_ARM64_REG_X20,UC_ARM64_REG_X21,UC_ARM64_REG_X22,UC_ARM64_REG_X23,UC_ARM64_REG_X24,UC_ARM64_REG_X25,UC_ARM64_REG_X26,UC_ARM64_REG_X27,UC_ARM64_REG_X28,UC_ARM64_REG_X29]
    result=[];event=None;writes=[];profile=None
    def code(uc,addr,size,_):
        nonlocal event,profile
        at=addr-bias
        if at in (0x1f01c,0xa5e2c,0x89fa54,0x8a5944,0x69f6bc):
            if at==0xa5e2c and uc.reg_read(UC_ARM64_REG_X30)==bias+0x8a3414:
                event=[uc.reg_read(r) for r in (UC_ARM64_REG_X2,UC_ARM64_REG_X3,UC_ARM64_REG_X4,UC_ARM64_REG_X5,UC_ARM64_REG_X6,UC_ARM64_REG_X7)]
            if at==0x69f6bc:profile=(uc.reg_read(UC_ARM64_REG_X2),uc.reg_read(UC_ARM64_REG_X3))
            if at==0x8a5944:
                for r in (UC_ARM64_REG_X2,UC_ARM64_REG_X3):assert bytes(uc.mem_read(uc.reg_read(r),4))==bytes(4)
            if at!=0x1f01c:uc.reg_write(UC_ARM64_REG_X0,0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:assert 0x8a2d44<=at<0x8a36d8,hex(at)
    def write(uc,kind,addr,size,value,_):
        if mmio<=addr<mmio+0x5000:
            assert addr==mmio+0x120 and size==4
            writes.append(value)
        else:assert (obj+0x7000<=addr and addr+size<=obj+0x8000 or bias+0x1aa4c00<=addr and addr+size<=bias+0x1aa4c10),hex(addr)
    uc.hook_add(UC_HOOK_CODE,code);uc.hook_add(UC_HOOK_MEM_WRITE,write)
    for port in ports:
        for slave in (0,1):
            for low in (0,3,0x5a6,0xfff):
                event=profile=None;writes.clear()
                va=0x12345000|low;pa=0xdeadbeef
                uc.mem_write(mmio,bytes(0x5000))
                uc.mem_write(mmio+0x120,struct.pack('<I',0xa55a006f))
                uc.mem_write(mmio+0x134,struct.pack('<I',1<<(slave*7)))
                uc.mem_write(mmio+0x13c+slave*8,struct.pack('<II',va,pa))
                uc.mem_write(mmio+0x150+slave*4,struct.pack('<I',port['tfid']|3))
                uc.mem_write(bias+0x1aa4c00,bytes(16))
                for i,r in enumerate(saved):uc.reg_write(r,0x12340000+i)
                for r,v in ((UC_ARM64_REG_X0,73),(UC_ARM64_REG_X1,0),(UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop)):uc.reg_write(r,v)
                uc.emu_start(bias+0x8a2d44,stop,count=6000)
                assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_X0)==1
                assert uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
                assert [uc.reg_read(r) for r in saved]==[0x12340000+i for i in range(len(saved))]
                assert event==[port['larb'],va&0xfffff000,pa,low&1,(low>>1)&1,port['tfid']|3],event
                assert profile==(port['index'],va&0xfffff000)
                assert writes==[0xa55a106f],writes
                result.append(dict(**port,slave=slave,raw_va=va,pa=pa,report=event))
    return result

MODEL = r'''
#define FIELD_GET(mask,value) (((value)&(mask))>>__builtin_ctzl(mask))
#define IRQ_HANDLED 1
#define IOMMU_FAULT_READ 0
#define IOMMU_FAULT_WRITE 1
#define str_write_read(x) ((x)?"write":"read")
struct mtk_iommu_data {
    const struct mtk_iommu_plat_data *plat_data;
    struct mtk_iommu_bank_data *bank;
};
static u32 regs[0x1000/4];
static unsigned int reports,logs,writes,barriers,offsets[8];
static u32 values[8];
static u64 reported_iova;
static int reported_write,report_result;
static struct { u32 status,id;u64 iova,pa;unsigned int larb,port;bool layer,write; } logged;
static struct mtk_iommu_bank_data *current;
static bool locked;
static u32 readl_relaxed(const void *p) {
    assert((const u32 *)p>=regs && (const u32 *)p<regs+ARRAY_SIZE(regs));
    return *(const u32 *)p;
}
static void writel_relaxed(u32 value,void *p) {
    assert((u32 *)p>=regs && (u32 *)p<regs+ARRAY_SIZE(regs) && writes<ARRAY_SIZE(values));
    unsigned int off=(u32 *)p-regs;
    offsets[writes]=off*4;values[writes++]=value;regs[off]=value;
}
#define spin_lock_irqsave(lock,flags) do { assert(!locked);locked=true;(void)(lock);(flags)=0; } while(0)
#define spin_unlock_irqrestore(lock,flags) do { assert(locked);locked=false;(void)(lock);(void)(flags); } while(0)
static void wmb(void) { assert(locked);barriers++; }
static int report_iommu_fault(struct iommu_domain *dom,struct device *dev,u64 iova,int write) {
    assert(current->m4u_dom && dom==&current->m4u_dom->domain && dev==current->parent_dev);
    reports++;reported_iova=iova;reported_write=write;return report_result;
}
static void dev_err_ratelimited(struct device *dev,const char *fmt,u32 status,u64 iova,u64 pa,u32 id,unsigned int larb,unsigned int port,bool layer,const char *operation) {
    (void)fmt;assert(dev==current->parent_dev);
    logs++;logged.status=status;logged.iova=iova;logged.pa=pa;logged.id=id;
    logged.larb=larb;logged.port=port;logged.layer=layer;logged.write=!strcmp(operation,"write");
}
'''
MAIN = r'''
static void fault(u32 status,u32 rawva,u32 pa,unsigned int larb,unsigned int port,u32 id,int disposition,bool trace) {
    struct device dev={0};struct mtk_iommu_domain dom={0};
    struct mtk_iommu_bank_data bank={.base=regs,.parent_dev=&dev,.irq=73};
    struct mtk_iommu_data data={.plat_data=&mt6765_data,.bank=&bank};
    bank.parent_data=&data;bank.m4u_dom=disposition<0?NULL:&dom;current=&bank;
    unsigned int slave=(status&0x7f)?0:1;
    for(unsigned int i=0;i<ARRAY_SIZE(regs);i++)regs[i]=0xa5a5a5a5;
    regs[0x134/4]=status;regs[(0x13c+8*slave)/4]=rawva;
    regs[(0x140+8*slave)/4]=pa;regs[(0x150+4*slave)/4]=id;
    reports=logs=writes=barriers=0;report_result=disposition;
    assert(mtk_iommu_isr(73,&bank)==IRQ_HANDLED);
    assert(reports==(disposition<0?0:1) && logs==(disposition==0?0:1));
    if(reports)assert(reported_iova==(rawva&0xfffff000) && reported_write==!!(rawva&2));
    if(logs) {
        assert(logged.status==status && logged.iova==(rawva&0xfffff000) && logged.pa==pa && logged.id==id);
        assert(logged.larb==larb && logged.port==port && logged.layer==!!(rawva&1) && logged.write==!!(rawva&2));
        if(trace)printf("FAULT %u %llu %llu %u %u %u\n",logged.larb,(unsigned long long)logged.iova,(unsigned long long)logged.pa,logged.layer,logged.write,logged.id);
    }
    assert(writes==3 && offsets[0]==0x120 && values[0]==0xa5a5b5a5);
    assert(offsets[1]==0x38 && values[1]==3 && offsets[2]==0x20 && values[2]==2);
    assert(barriers==1 && !locked);
}
int main(int argc,char **argv) {
    assert(argc==1 || argc==7);
    if(argc==7) {
        fault(strtoul(argv[1],NULL,0),strtoul(argv[2],NULL,0),strtoul(argv[3],NULL,0),
              strtoul(argv[4],NULL,0),strtoul(argv[5],NULL,0),strtoul(argv[6],NULL,0),1,true);
        return 0;
    }
    const unsigned int counts[]={8,11,12,21};
    unsigned int cases=0;
    for(unsigned int larb=0;larb<4;larb++)for(unsigned int port=0;port<counts[larb];port++)
        for(unsigned int slave=0;slave<2;slave++)for(unsigned int low=0;low<4096;low++) {
            fault(1U<<(7*slave),0xfffff000|low,0xdeadbeef,larb,port,(larb<<7)|(port<<2)|3,1,false);cases++;
        }
    for(int disposition=-1;disposition<=1;disposition++) {
        fault(1,0x1003,0xffffffff,0,1,4,disposition,false);
        fault(0x80,0x3,0xabcdef12,3,20,(3<<7)|(20<<2),disposition,false);
    }
    printf("PASS: %u native fault reports across both MMU slaves and all 52 ports; callback, missing-domain, acknowledgement and full-flush paths\n",cases+6);
}
'''

def build_harness(source):
    s = source.read_text()
    code = setup.PRELUDE+s[s.index('#define REG_MMU_PT_BASE_ADDR'):s.index('enum mtk_iommu_plat')]
    for name in ('mtk_iommu_plat','mtk_iommu_iova_region','mtk_iommu_plat_data','mtk_iommu_bank_data','single_domain','mt6765_data'):
        code += smi.block(s,name)
    code += MODEL
    for name in ('mtk_iommu_tlb_flush_all','mtk_iommu_isr'): code += smi.block(s,name)
    code += MAIN
    path = ROOT/'out/mt6765-fault-host.c'; path.write_text(code)
    binary = ROOT/'out/mt6765-fault-host'
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-sign-compare',
        '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g','-I'+str(SRC/'include'),str(path),'-o',str(binary)],check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,default=SRC/'drivers/iommu/mtk_iommu.c')
    args = parser.parse_args()
    if not args.source.resolve().is_relative_to(ROOT): raise SystemExit('Source must remain under /rabbitr1')
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    binary = build_harness(args.source)
    subprocess.run([str(binary)],check=True)
    fixtures = stock_faults(raw)
    for f in fixtures:
        output = subprocess.check_output([str(binary),str(1<<(f['slave']*7)),str(f['raw_va']),str(f['pa']),str(f['larb']),str(f['port']),str(f['tfid']|3)],text=True)
        native = list(map(int,output.split()[1:]))
        assert native == f['report'], (native,f)
        f['native_report'] = native
    (ROOT/'out/mt6765-fault-audit.json').write_text(json.dumps(dict(
        raw_offset='0x8a2d44',fixtures=fixtures,hardware_tested=False),indent=2)+'\n')
    print(f'PASS: {len(fixtures)} stock fault reports match native decoding, with actual stock port-table lookup and IRQ clear')
    print('MMIO, diagnostic helpers and response callbacks are modeled. IRQ routing, lifetime and hardware faults remain untested.')


if __name__ == '__main__': main()
