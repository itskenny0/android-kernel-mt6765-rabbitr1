#!/usr/bin/env python3
"""Audit MT6765 address formats against the shipped code and real page-table operations."""
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

funcs={4096:(0x8accac,0x8ad0c4),65536:(0x8ac86c,0x8accac),1048576:(0x8ac568,0x8ac86c),16777216:(0x8ac21c,0x8ac568)}
def stock_map(raw,size,pa,va):
    bias,obj,table,stop=0x1000000,0x20000000,0xffffffc030000000,0x40000000
    start,end=funcs[size]
    uc=Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias,0x2000000);uc.mem_write(bias,raw)
    uc.mem_map(obj,0x20000);uc.mem_map(table,0x1000);uc.mem_map(stop,0x1000)
    uc.mem_write(obj,struct.pack('<Q',obj+0x10000))
    uc.mem_write(bias+0x148d968,bytes(8))
    uc.mem_write(bias+0x1aa47c4,bytes(4))
    if size<1048576:uc.mem_write(obj+0x10000+(va>>20)*4,struct.pack('<I',0x30000009))
    saved=[UC_ARM64_REG_X19,UC_ARM64_REG_X20,UC_ARM64_REG_X21,UC_ARM64_REG_X22,UC_ARM64_REG_X23,UC_ARM64_REG_X24,UC_ARM64_REG_X25,UC_ARM64_REG_X26,UC_ARM64_REG_X27,UC_ARM64_REG_X28,UC_ARM64_REG_X29]
    for i,r in enumerate(saved):uc.reg_write(r,0x12340000+i)
    for r,v in [(UC_ARM64_REG_X0,obj),(UC_ARM64_REG_X1,va),(UC_ARM64_REG_X2,pa),(UC_ARM64_REG_X3,8), (UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop)]:uc.reg_write(r,v)
    writes=[];locked=False
    def code(uc,addr,sz,_):
        nonlocal locked
        at=addr-bias
        if at in (0x1f01c,0xa5e2c,0xf342ec,0xf3435c):
            if at in (0xf342ec,0xf3435c):
                assert uc.reg_read(UC_ARM64_REG_X0)==obj+16
                assert locked==(at==0xf3435c);locked=not locked
            if at!=0x1f01c:uc.reg_write(UC_ARM64_REG_X0,0)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:assert start<=at<end,hex(at)
    def write(uc,access,addr,sz,v,_):
        if obj+0x7000<=addr and addr+sz<=obj+0x8000:return
        assert locked and sz==4
        assert (obj+0x10000<=addr<obj+0x14000 or table<=addr<table+0x400),hex(addr)
        writes.append((addr,v))
    uc.hook_add(UC_HOOK_CODE,code);uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+start,stop,count=4000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_X0)==0 and not locked
    assert uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert [uc.reg_read(r) for r in saved]==[0x12340000+i for i in range(len(saved))]
    return writes

def stock_root(raw,pa):
    bias,obj,stop=0x1000000,0x20000000,0x40000000
    uc=Uc(UC_ARCH_ARM64,UC_MODE_ARM)
    uc.mem_map(bias,0x2000000);uc.mem_write(bias,raw)
    uc.mem_map(obj,0x20000);uc.mem_map(stop,0x1000)
    uc.mem_write(obj+0x10000,b'\xa5'*0x4000)
    saved=[UC_ARM64_REG_X19,UC_ARM64_REG_X20,UC_ARM64_REG_X21,UC_ARM64_REG_X22,UC_ARM64_REG_X23,UC_ARM64_REG_X24,UC_ARM64_REG_X25,UC_ARM64_REG_X26,UC_ARM64_REG_X27,UC_ARM64_REG_X28,UC_ARM64_REG_X29]
    for i,r in enumerate(saved):uc.reg_write(r,0x12340000+i)
    for r,v in [(UC_ARM64_REG_X0,obj),(UC_ARM64_REG_X1,obj+0x100),(UC_ARM64_REG_SP,obj+0x8000),(UC_ARM64_REG_X30,stop)]:uc.reg_write(r,v)
    calls=[]
    def code(uc,addr,size,_):
        at=addr-bias
        if at in (0x1f01c,0xa5e2c,0xc0380,0xf1e480,0x8abe84,0x279880):
            ret=0
            if at==0xc0380:
                assert uc.reg_read(UC_ARM64_REG_X0)==0 and uc.reg_read(UC_ARM64_REG_X1)==0x4000
                assert uc.reg_read(UC_ARM64_REG_X2)==obj+0x108
                uc.mem_write(obj+0x108,struct.pack('<Q',pa))
                uc.mem_write(uc.reg_read(UC_ARM64_REG_X3),struct.pack('<Q',obj+0x10000))
                ret=1
            elif at==0xf1e480:
                assert [uc.reg_read(r) for r in (UC_ARM64_REG_X0,UC_ARM64_REG_X1,UC_ARM64_REG_X2)]==[obj+0x10000,0,0x4000]
                uc.mem_write(obj+0x10000,bytes(0x4000));ret=obj+0x10000
            calls.append(at)
            if at!=0x1f01c:uc.reg_write(UC_ARM64_REG_X0,ret)
            uc.reg_write(UC_ARM64_REG_PC,uc.reg_read(UC_ARM64_REG_X30))
        else:assert 0x8ada68<=at<0x8adbdc,hex(at)
    def write(uc,kind,addr,size,value,_):
        assert (obj+0x7000<=addr and addr+size<=obj+0x8000 or obj+0x100<=addr and addr+size<=obj+0x110),hex(addr)
    uc.hook_add(UC_HOOK_CODE,code);uc.hook_add(UC_HOOK_MEM_WRITE,write)
    uc.emu_start(bias+0x8ada68,stop,count=2000)
    assert uc.reg_read(UC_ARM64_REG_PC)==stop and uc.reg_read(UC_ARM64_REG_X0)==0
    assert uc.reg_read(UC_ARM64_REG_SP)==obj+0x8000
    assert [uc.reg_read(r) for r in saved]==[0x12340000+i for i in range(len(saved))]
    assert bytes(uc.mem_read(obj+0x10000,0x4000))==bytes(0x4000)
    assert struct.unpack('<QQ',uc.mem_read(obj+0x100,16))==(obj+0x10000,pa)
    assert calls.count(0xc0380)==calls.count(0xf1e480)==calls.count(0x8abe84)==1
    return dict(pa=pa,bytes=0x4000,calls=calls)

PRELUDE = r'''
#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dt-bindings/memory/mtk-memory-port.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t u64;
typedef uint64_t phys_addr_t;
typedef uint64_t dma_addr_t;
typedef unsigned int gfp_t;
typedef unsigned int slab_flags_t;
#define __iomem
#define __maybe_unused
#define CONFIG_PHYS_ADDR_T_64BIT 1
#define CONFIG_ZONE_DMA32 1
#define IS_ENABLED(x) (x)
#define BIT(n) (1UL<<(n))
#define BIT_ULL(n) (1ULL<<(n))
#define GENMASK(h,l) ((~0UL<<(l)) & (~0UL>>(63-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define lower_32_bits(n) ((u32)(n))
#define upper_32_bits(n) ((u32)((uint64_t)(n)>>32))
#define SZ_4K (1UL<<12)
#define SZ_64K (1UL<<16)
#define SZ_1M (1UL<<20)
#define SZ_8M (1UL<<23)
#define SZ_16M (1UL<<24)
#define SZ_4G (1ULL<<32)
#define MTK_IOMMU_IOVA_SZ_4G (SZ_4G-SZ_8M)
#define GFP_KERNEL 1U
#define GFP_DMA32 2U
#define __GFP_ZERO 4U
#define SLAB_CACHE_DMA32 8U
#define IOMMU_READ 1
#define IOMMU_WRITE 2
#define IOMMU_CACHE 4
#define IOMMU_NOEXEC 8
#define IOMMU_MMIO 16
#define IOMMU_PRIV 32
#define DMA_TO_DEVICE 1
#define ARM_V7S 0
#define dev_err(...) ((void)0)
#define dev_warn(...) ((void)0)
#define WARN_ON(x) warn(!!(x))
#define WARN_ONCE(x,...) WARN_ON(x)
#define READ_ONCE(x) (x)
#define container_of(p,t,m) ((t *)((char *)(p)-offsetof(t,m)))
#define kmalloc_obj(x) calloc(1,sizeof(x))
#define kfree free
#define kmemleak_ignore(x) ((void)0)
#define io_pgtable_ops_to_pgtable(x) container_of(x,struct io_pgtable,ops)
struct device { int unused; };
struct iommu_iotlb_gather { bool queued; };
struct io_pgtable_ops {
    int (*map_pages)(struct io_pgtable_ops *,unsigned long,phys_addr_t,size_t,size_t,int,gfp_t,size_t *);
    size_t (*unmap_pages)(struct io_pgtable_ops *,unsigned long,size_t,size_t,struct iommu_iotlb_gather *);
    phys_addr_t (*iova_to_phys)(struct io_pgtable_ops *,unsigned long);
};
struct io_pgtable_cfg {
    unsigned int quirks,ias,oas;
    unsigned long pgsize_bitmap;
    bool coherent_walk;
    struct device *iommu_dev;
    struct { u32 ttbr,tcr,prrr,nmrr; } arm_v7s_cfg;
};
struct io_pgtable { struct io_pgtable_ops ops; struct io_pgtable_cfg cfg; void *cookie; };
struct iommu_domain { unsigned long pgsize_bitmap; struct { u64 aperture_start,aperture_end; bool force_aperture; } geometry; };
struct mtk_iommu_domain { struct mtk_iommu_bank_data *bank; struct iommu_domain domain; struct io_pgtable_cfg cfg; struct io_pgtable_ops *iop; };
struct list_head { int unused; };
struct kmem_cache { size_t size; unsigned int flags; };
static unsigned int warns,barriers,syncs,tlbs,allocs,dma_maps,dma_unmaps;
static bool warn(bool value) { warns+=value;return value; }
static bool fail_alloc;
static u64 root_pa=0x48004000,l2_pa=0x49000400;
static unsigned int root_flags,slab_flags;
struct allocation { void *p; size_t size; u64 pa; };
static struct allocation allocations[64];
static void *allocate(size_t size,u64 pa) {
    if(fail_alloc)return NULL;
    assert(allocs<ARRAY_SIZE(allocations));
    void *p=calloc(1,size);assert(p);
    allocations[allocs++]=(struct allocation){p,size,pa};return p;
}
static void release(void *p) {
    unsigned int n;
    for(n=0;n<allocs;n++)if(allocations[n].p==p)break;
    assert(n<allocs);free(p);allocations[n]=allocations[--allocs];
}
static u64 virt_to_phys(const void *p) {
    uintptr_t v=(uintptr_t)p;
    for(unsigned int n=0;n<allocs;n++) {
        struct allocation *a=&allocations[n];uintptr_t start=(uintptr_t)a->p;
        if(v>=start && v<start+a->size)return a->pa+v-start;
    }
    assert(!"unknown table address");return 0;
}
static void *phys_to_virt(u64 pa) {
    for(unsigned int n=0;n<allocs;n++) {
        struct allocation *a=&allocations[n];
        if(pa>=a->pa && pa<a->pa+a->size)return (char *)a->p+(pa-a->pa);
    }
    return NULL; /* A leaf PA need not describe a software page table. */
}
static unsigned int get_order(size_t n) { unsigned int order=0;while((4096UL<<order)<n)order++;return order; }
static unsigned long __get_free_pages(gfp_t flags,unsigned int order) {
    root_flags=flags;return (unsigned long)allocate(4096UL<<order,root_pa);
}
static void free_pages(unsigned long p,unsigned int order) { (void)order;release((void *)p); }
static struct kmem_cache *kmem_cache_create(const char *name,size_t size,size_t align,unsigned int flags,void *ctor) {
    (void)name;(void)ctor;assert(size==align);slab_flags=flags;
    struct kmem_cache *cache=malloc(sizeof(*cache));assert(cache);
    *cache=(struct kmem_cache){size,flags};return cache;
}
static void *kmem_cache_zalloc(struct kmem_cache *cache,gfp_t gfp) { (void)gfp;return allocate(cache->size,l2_pa); }
static void kmem_cache_free(struct kmem_cache *cache,void *p) { (void)cache;release(p); }
#define kmem_cache_destroy free
static dma_addr_t dma_map_single(struct device *dev,void *p,size_t size,int direction) {
    (void)dev;(void)size;assert(direction==DMA_TO_DEVICE);dma_maps++;return virt_to_phys(p);
}
static bool dma_mapping_error(struct device *dev,dma_addr_t pa) { (void)dev;(void)pa;return false; }
static void dma_unmap_single(struct device *dev,dma_addr_t pa,size_t size,int direction) {
    (void)dev;(void)pa;(void)size;assert(direction==DMA_TO_DEVICE);dma_unmaps++;
}
static void dma_sync_single_for_device(struct device *dev,dma_addr_t pa,size_t size,int direction) {
    (void)dev;assert(phys_to_virt(pa) && size && direction==DMA_TO_DEVICE);syncs++;
}
static u32 cmpxchg_relaxed(u32 *p,u32 old,u32 value) { u32 result=*p;if(result==old)*p=value;return result; }
static void wmb(void) { barriers++; }
#define dma_wmb wmb
static bool iommu_iotlb_gather_queued(struct iommu_iotlb_gather *g) { return g && g->queued; }
static void io_pgtable_tlb_flush_walk(struct io_pgtable *iop,unsigned long iova,size_t size,size_t granule) {
    (void)iop;assert(!(iova&(granule-1)) && size>=granule);tlbs++;
}
static void io_pgtable_tlb_add_page(struct io_pgtable *iop,struct iommu_iotlb_gather *g,unsigned long iova,size_t granule) {
    (void)iop;(void)g;assert(!(iova&(granule-1)));tlbs++;
}
'''
WRAPPER = r'''
struct mtk_iommu_bank_data { struct mtk_iommu_data *parent_data; };
struct mtk_iommu_data { struct device *dev; const struct mtk_iommu_plat_data *plat_data; bool enable_4GB; struct mtk_iommu_domain *share_dom; };
static struct io_pgtable_ops *alloc_io_pgtable_ops(int fmt,struct io_pgtable_cfg *cfg,void *cookie) {
    assert(fmt==ARM_V7S);struct io_pgtable *iop=arm_v7s_alloc_pgtable(cfg,cookie);
    if(!iop)return NULL;
    iop->cfg=*cfg;iop->cookie=cookie;return &iop->ops;
}
'''
MAIN = r'''
static struct mtk_iommu_domain domain(const struct mtk_iommu_plat_data *pdata) {
    static struct device dev;
    static struct mtk_iommu_data data;
    static struct mtk_iommu_bank_data bank;
    data=(struct mtk_iommu_data){.dev=&dev,.plat_data=pdata};
    bank.parent_data=&data;
    struct mtk_iommu_domain dom={.bank=&bank,.domain.pgsize_bitmap=SZ_4K|SZ_64K|SZ_1M|SZ_16M};
    assert(!mtk_iommu_domain_finalise(&dom,&data,0));
    assert(dom.cfg.ias==32 && dom.cfg.oas==(pdata==&mt6765_data?34:35));
    assert(dom.domain.geometry.aperture_start==0 && dom.domain.geometry.aperture_end==0xff7fffff);
    assert(dom.domain.geometry.force_aperture);
    assert(dom.cfg.arm_v7s_cfg.ttbr==root_pa);
    bool high=pdata==&mt6779_data;
    assert(!!(root_flags&GFP_DMA32)==!high && (root_flags&__GFP_ZERO));
    assert(slab_flags==(high?0:SLAB_CACHE_DMA32));
    assert(!!(dom.cfg.quirks&IO_PGTABLE_QUIRK_ARM_MTK_TTBR_EXT)==high);
    struct arm_v7s_io_pgtable *pt=io_pgtable_to_data(io_pgtable_ops_to_pgtable(dom.iop));
    assert(ARM_V7S_TABLE_SIZE(1,&pt->iop.cfg)==16384);
    struct mtk_iommu_domain shared={0};
    unsigned int before=allocs;
    assert(!mtk_iommu_domain_finalise(&shared,&data,0));
    assert(shared.iop==dom.iop && shared.cfg.ias==32 && allocs==before);
    return dom;
}
static void unmap_free(struct mtk_iommu_domain *dom,u64 va,size_t size) {
    assert(dom->iop->unmap_pages(dom->iop,va,size,1,NULL)==size);
    assert(!dom->iop->iova_to_phys(dom->iop,va));
    arm_v7s_free_pgtable(io_pgtable_ops_to_pgtable(dom->iop));assert(!allocs);
}
static void map_case(size_t size,u64 pa,u64 va,bool trace) {
    struct mtk_iommu_domain dom=domain(&mt6765_data);
    size_t mapped=0;unsigned int before=syncs,barrier=barriers;
    assert(!dom.iop->map_pages(dom.iop,va,pa,size,1,IOMMU_READ|IOMMU_WRITE,GFP_KERNEL,&mapped));
    assert(mapped==size && syncs>before && barriers>barrier);
    assert(dom.iop->iova_to_phys(dom.iop,va)==pa);
    assert(dom.iop->iova_to_phys(dom.iop,va+size-1)==pa+size-1);
    assert(dom.iop->iova_to_phys(dom.iop,va+17)==pa+17);
    struct arm_v7s_io_pgtable *pt=io_pgtable_to_data(io_pgtable_ops_to_pgtable(dom.iop));
    u32 *entry=pt->pgd+(va>>20);
    if(size<SZ_1M)entry=iopte_deref(*entry,1,pt)+((va>>12)&255);
    size_t count=(size==SZ_64K || size==SZ_16M)?16:1;
    if(trace)for(size_t i=0;i<count;i++)printf("PTE %u\n",entry[i]);
    before=warns;mapped=0;
    assert(dom.iop->map_pages(dom.iop,va,pa,size,1,IOMMU_READ|IOMMU_WRITE,GFP_KERNEL,&mapped)==-EEXIST);
    assert(!mapped && warns==before+1);
    unmap_free(&dom,va,size);
}
static void boundaries(void) {
    struct mtk_iommu_domain dom=domain(&mt6765_data);
    size_t mapped=0;unsigned int before=syncs;
    assert(dom.iop->map_pages(dom.iop,0x10000000,1ULL<<34,SZ_4K,1,IOMMU_READ,GFP_KERNEL,&mapped)==-ERANGE);
    assert(dom.iop->map_pages(dom.iop,1ULL<<32,0x50000000,SZ_4K,1,IOMMU_READ,GFP_KERNEL,&mapped)==-ERANGE);
    assert(!mapped && syncs==before);
    fail_alloc=true;
    assert(dom.iop->map_pages(dom.iop,0x10000000,0x50000000,SZ_4K,1,IOMMU_READ,GFP_KERNEL,&mapped)==-ENOMEM);
    fail_alloc=false;assert(!mapped && allocs==1);
    l2_pa=0x149000400;
    assert(dom.iop->map_pages(dom.iop,0x10000000,0x50000000,SZ_4K,1,IOMMU_READ,GFP_KERNEL,&mapped)==-ENOMEM);
    assert(!mapped && allocs==1);l2_pa=0x49000400;
    assert(!dom.iop->map_pages(dom.iop,0xfffff000,0x3fffff000,SZ_4K,1,IOMMU_READ,GFP_KERNEL,&mapped));
    assert(dom.iop->iova_to_phys(dom.iop,0xffffffff)==0x3ffffffffULL);
    /* The raw format supports 32 IOVA bits; the driver reserves the top 8 MiB. */
    unmap_free(&dom,0xfffff000,SZ_4K);
    struct device dev={0};struct mtk_iommu_data data={.dev=&dev,.plat_data=&mt6765_data};
    memset(&dom,0,sizeof(dom));dom.domain.pgsize_bitmap=SZ_4K;
    root_pa=0x148004000;
    assert(mtk_iommu_domain_finalise(&dom,&data,0)==-ENOMEM && !allocs && !data.share_dom);
    root_pa=0x48004000;fail_alloc=true;
    assert(mtk_iommu_domain_finalise(&dom,&data,0)==-ENOMEM && !allocs && !data.share_dom);
    fail_alloc=false;
}
static void batch_boundaries(void) {
    const size_t sizes[]={SZ_4K,SZ_64K,SZ_1M,SZ_16M};
    for(unsigned int n=0;n<ARRAY_SIZE(sizes);n++) {
        struct mtk_iommu_domain dom=domain(&mt6765_data);
        size_t size=sizes[n],mapped=123;
        unsigned int before=syncs;
        assert(mtk_iommu_map(&dom.domain,0x10000000,(1ULL<<34)-size,size,2,IOMMU_READ,GFP_KERNEL,&mapped)==-ERANGE);
        u64 last=(0xff800000ULL/size-1)*size;
        size_t cross=0xff800000ULL%size?3:2;
        assert(mtk_iommu_map(&dom.domain,last,0x50000000,size,cross,IOMMU_READ,GFP_KERNEL,&mapped)==-ERANGE);
        assert(mtk_iommu_map(&dom.domain,0x10000000,0x50000000,size,SIZE_MAX/size+1,IOMMU_READ,GFP_KERNEL,&mapped)==-ERANGE);
        assert(mtk_iommu_map(&dom.domain,0x10000000,0x50000000,0,1,IOMMU_READ,GFP_KERNEL,&mapped)==-EINVAL);
        assert(mtk_iommu_map(&dom.domain,0x10000000,0x50000000,size,0,IOMMU_READ,GFP_KERNEL,&mapped)==-EINVAL);
        assert(mapped==123 && syncs==before && allocs==1);
        mapped=0;
        assert(!mtk_iommu_map(&dom.domain,0x10000000,(1ULL<<34)-2*size,size,2,IOMMU_READ,GFP_KERNEL,&mapped));
        assert(mapped==2*size);
        assert(dom.iop->iova_to_phys(dom.iop,0x10000000)==(1ULL<<34)-2*size);
        assert(dom.iop->iova_to_phys(dom.iop,0x10000000+2*size-1)==(1ULL<<34)-1);
        assert(dom.iop->unmap_pages(dom.iop,0x10000000,size,2,NULL)==2*size);
        arm_v7s_free_pgtable(io_pgtable_ops_to_pgtable(dom.iop));assert(!allocs);
    }
}
int main(int argc,char **argv) {
    assert(argc==1 || argc==4);
    if(argc==4) {
        map_case(strtoul(argv[1],NULL,0),strtoull(argv[2],NULL,0),strtoull(argv[3],NULL,0),true);
        return 0;
    }
    const size_t sizes[]={SZ_4K,SZ_64K,SZ_1M,SZ_16M};
    for(unsigned int upper=0;upper<4;upper++)
        for(unsigned int i=0;i<ARRAY_SIZE(sizes);i++)map_case(sizes[i],0x45000000ULL+(upper*SZ_4G),0x12000000,false);
    boundaries();batch_boundaries();
    const struct mtk_iommu_plat_data *legacy[]={&mt8183_data,&mt6779_data};
    for(unsigned int n=0;n<ARRAY_SIZE(legacy);n++) {
        struct mtk_iommu_domain dom=domain(legacy[n]);size_t mapped=0;
        assert(!dom.iop->map_pages(dom.iop,0x12345000,0x740005000ULL,SZ_4K,1,IOMMU_READ,GFP_KERNEL,&mapped));
        assert(dom.iop->iova_to_phys(dom.iop,0x12345004)==0x740005004ULL);
        unmap_free(&dom,0x12345000,SZ_4K);
    }
    assert(dma_maps==dma_unmaps && !allocs);
    puts("PASS: native domain configuration, all four page sizes, high physical bits, mapping/unmapping, complete batch bounds and allocation failures; legacy formats preserved");
}
'''

def build_harness(source, pgtable):
    s = source.read_text(); p = pgtable.read_text()
    code = PRELUDE
    header = (SRC/'include/linux/io-pgtable.h').read_text()
    code += '\n'.join(line for line in header.splitlines() if line.lstrip().startswith('#define IO_PGTABLE_QUIRK'))+'\n'
    # Compile the production allocator and complete map/unmap/walk implementation.
    code += p[p.index('#define io_pgtable_to_data'):p.index('struct io_pgtable_init_fns io_pgtable_arm_v7s_init_fns')]
    code += s[s.index('#define REG_MMU_PT_BASE_ADDR'):s.index('enum mtk_iommu_plat')]
    for name in ('mtk_iommu_plat','mtk_iommu_iova_region','mtk_iommu_plat_data','single_domain','mt6765_data','mt8183_data','mt6779_data'):
        code += smi.block(s,name)
    code += WRAPPER
    for name in ('mtk_iommu_domain_finalise','to_mtk_domain','mtk_iommu_map'): code += smi.block(s,name)
    code += MAIN
    path = ROOT/'out/mt6765-address-host.c'; path.write_text(code)
    binary = ROOT/'out/mt6765-address-host'
    subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-unused-parameter','-Wno-sign-compare',
        '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g','-I'+str(SRC/'include'),str(path),'-o',str(binary)],check=True)
    return binary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source', type=Path, default=SRC/'drivers/iommu/mtk_iommu.c')
    parser.add_argument('--pgtable', type=Path, default=SRC/'drivers/iommu/io-pgtable-arm-v7s.c')
    args = parser.parse_args()
    if any(not path.resolve().is_relative_to(ROOT) for path in (args.source,args.pgtable)):
        raise SystemExit('Sources must remain under /rabbitr1')
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    binary = build_harness(args.source,args.pgtable)
    subprocess.run([str(binary)],check=True)
    roots = [stock_root(raw,pa) for pa in (0x48000000,0x48004000,0x48008000)]
    fixtures = []
    for size in funcs:
        for high in range(4):
            for va,base in ((0x12000000,0x45000000),(0,0),(0xff000000,0xff000000)):
                pa = base+(high<<32)
                writes = stock_map(raw,size,pa,va)
                output = subprocess.check_output([str(binary),str(size),str(pa),str(va)],text=True)
                native = [int(line.split()[1]) for line in output.splitlines()]
                assert len(native) == len(writes) == (16 if size in (65536,16777216) else 1)
                # The generic ARM format also sets nG and normal-memory TEX.
                # Compare every bit, recording those intentional attribute differences.
                attrs = 0x840 if size == 4096 else 0x1800 if size == 65536 else 0x21000
                assert all(a ^ b[1] == attrs for a,b in zip(native,writes)), (size,pa,native,writes)
                fixtures.append(dict(size=size,pa=pa,iova=va,stock=writes,native=native,extra_attributes=attrs))
    (ROOT/'out/mt6765-address-audit.json').write_text(json.dumps(dict(
        roots=roots,mappings=fixtures,hardware_tested=False),indent=2)+'\n')
    print(f'PASS: {len(roots)} stock root allocations and {len(fixtures)} stock mappings; address/type/share/security/cache bits match')
    print('The generic nG/TEX attributes are recorded separately. No hardware DMA, coherency or table walks are emulated.')


if __name__ == '__main__': main()
