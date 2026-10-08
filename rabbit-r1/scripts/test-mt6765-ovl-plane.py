#!/usr/bin/env python3
"""Compare RGB plane setup with shipped code; check DRM opacity and reflection."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import resource
import struct
import subprocess
from unicorn import Uc, UC_ARCH_ARM64, UC_MODE_ARM, UC_HOOK_CODE, UC_HOOK_MEM_WRITE
from unicorn.arm64_const import (UC_ARM64_REG_PC, UC_ARM64_REG_SP, UC_ARM64_REG_X0,
    UC_ARM64_REG_X1, UC_ARM64_REG_X2, UC_ARM64_REG_X3, UC_ARM64_REG_X4,
    UC_ARM64_REG_X5, UC_ARM64_REG_X6, UC_ARM64_REG_X7, UC_ARM64_REG_X30)

ROOT = Path('/rabbitr1'); SRC = ROOT/'src/mainline'
resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--source', type=Path, default=SRC/'drivers/gpu/drm/mediatek/mtk_disp_ovl.c')
args = parser.parse_args()
if not args.source.resolve().is_relative_to(ROOT): raise SystemExit('Source must be under /rabbitr1')
# Vendor names describe memory byte order, DRM names describe little-endian words.
# These are the literal UFMT values from the shipped driver's color-format ABI.
# name, bytes/pixel, has_alpha, coverage UFMT, premultiplied UFMT
FORMATS = [
    ('XRGB8888', 4, False, 0xc00809, 0xc00c2d),
    ('ARGB8888', 4, True, 0xc00809, 0xc00c2d),
    ('BGRX8888', 4, False, 0xc00e0a, 0xc00e2a),
    ('BGRA8888', 4, True, 0xc00e0a, 0xc00e2a),
    ('ABGR8888', 4, True, 0xc00a08, 0xc00d2c),
    ('XBGR8888', 4, False, 0xc00a08, 0xc00d2c),
    ('RGB888', 3, False, 0xb00407, 0xb00407),
    ('BGR888', 3, False, 0xb00606, 0xb00606),
    ('RGB565', 2, False, 0xa00004, 0xa00004),
]


def stock(raw, module, layer, fmt, alpha, const_blend, seed, x, y, width, height, pitch,
          base=0x58000000):
    bias, obj, mmio, stop = 0x1000000, 0x20000000, 0x30000000, 0x40000000
    uc = Uc(UC_ARCH_ARM64, UC_MODE_ARM)
    uc.mem_map(bias, 0x2000000); uc.mem_write(bias, raw)
    uc.mem_map(obj, 0x10000); uc.mem_map(mmio, 0x1000); uc.mem_map(stop, 0x1000)
    cfg = bytearray(184)
    fields = {0:module, 4:layer, 8:1, 20:fmt, 48:x, 52:y, 56:width, 60:height,
        64:pitch, 68:5, 72:9, 92:width, 96:height, 108:1, 112:alpha, 156:1,
        164:const_blend, 168:0xffffffff, 172:0xffffffff, 176:layer}
    for off, value in fields.items(): struct.pack_into('<I', cfg, off, value)
    struct.pack_into('<Q', cfg, 24, base)
    uc.mem_write(obj, bytes(cfg)); uc.mem_write(mmio, struct.pack('<I', seed)*1024)
    # disp_rect consumes x2/x3; cfg is x5. NULL CMDQ handle is the ninth argument.
    for reg, value in zip([UC_ARM64_REG_X0, UC_ARM64_REG_X1, UC_ARM64_REG_X2,
            UC_ARM64_REG_X3, UC_ARM64_REG_X4, UC_ARM64_REG_X5, UC_ARM64_REG_X6,
            UC_ARM64_REG_X7], [module, layer, 0, (640<<32)|480, 0, obj, 0, 0]):
        uc.reg_write(reg, value)
    uc.reg_write(UC_ARM64_REG_SP, obj+0x8000); uc.reg_write(UC_ARM64_REG_X30, stop)
    writes = []
    def code(uc, address, size, _):
        at = address-bias
        if at in [0x1f01c, 0x743f94, 0x732548]:
            if at == 0x743f94:
                assert uc.reg_read(UC_ARM64_REG_X0) == module
                uc.reg_write(UC_ARM64_REG_X0, mmio)
            elif at == 0x732548: uc.reg_write(UC_ARM64_REG_X0, 0)
            uc.reg_write(UC_ARM64_REG_PC, uc.reg_read(UC_ARM64_REG_X30))
        else:
            assert (0x700d90 <= at < 0x702154 or 0x6f7010 <= at < 0x6f7088 or
                    0x744ea0 <= at < 0x745064), hex(at)
    def write(uc, access, address, size, value, _):
        if mmio <= address < mmio+0x1000:
            assert size == 4; writes.append((address-mmio, value))
        else:
            assert obj <= address < obj+len(cfg) or obj+0x7000 <= address < obj+0x8000, hex(address)
    uc.hook_add(UC_HOOK_CODE, code); uc.hook_add(UC_HOOK_MEM_WRITE, write)
    uc.emu_start(bias+0x700d90, stop, count=10000)
    # The static function's unused return value was optimized away in this build.
    assert uc.reg_read(UC_ARM64_REG_PC) == stop
    assert uc.reg_read(UC_ARM64_REG_SP) == obj+0x8000
    return writes


def block(source, start):
    begin = source.index(start); end = source.index('\n}', begin)+2
    if source[end:end+1] == ';': end += 1
    return source[begin:end]+'\n'


PRELUDE = r'''
#include <assert.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
typedef uint8_t u8;
typedef uint32_t u32;
typedef uint64_t dma_addr_t;
#define BIT(n) (1U<<(n))
#define GENMASK(h,l) ((~0U<<(l)) & (~0U>>(31-(h))))
#define ARRAY_SIZE(a) (sizeof(a)/sizeof((a)[0]))
#define __iomem
#define fourcc_code(a,b,c,d) ((u32)(a) | ((u32)(b)<<8) | ((u32)(c)<<16) | ((u32)(d)<<24))
#define DRM_FORMAT_MOD_LINEAR 0
struct device { void *data; };
struct cmdq_client_reg { int unused; };
struct drm_format_info { bool has_alpha,is_yuv; u8 cpp[4]; };
struct drm_framebuffer { const struct drm_format_info *format; };
struct drm_plane_state { struct drm_framebuffer *fb; unsigned int alpha,pixel_blend_mode; };
struct mtk_plane_pending_state {
    bool enable; dma_addr_t addr,hdr_addr; unsigned int pitch,hdr_pitch,format;
    uint64_t modifier; unsigned int x,y,src_x,width,height,rotation;
};
struct mtk_plane_state { struct drm_plane_state base; struct mtk_plane_pending_state pending; };
static void *dev_get_drvdata(struct device *dev) { return dev->data; }
static u32 regs[1024];
static unsigned int writes;
static bool trace;
struct queued { u32 off,val,mask; };
struct cmdq_pkt { struct queued ops[64]; unsigned int count; };
static void queue(struct cmdq_pkt *pkt,u32 val,void *base,u32 off,u32 mask)
{
    assert(base==regs && off%4==0 && off<sizeof(regs));
    if (pkt) { assert(pkt->count<64); pkt->ops[pkt->count++]=(struct queued){off,val,mask}; }
    else { regs[off/4]=(regs[off/4]&~mask)|(val&mask); writes++;
           if (trace) printf("W %x %x\n",off,regs[off/4]); }
}
static void mtk_ddp_write(struct cmdq_pkt *pkt,u32 val,struct cmdq_client_reg *reg,void *base,u32 off)
{ (void)reg; queue(pkt,val,base,off,~0U); }
#define mtk_ddp_write_relaxed mtk_ddp_write
static void mtk_ddp_write_mask(struct cmdq_pkt *pkt,u32 val,struct cmdq_client_reg *reg,void *base,u32 off,u32 mask)
{ (void)reg; queue(pkt,val,base,off,mask); }
static void flush(struct cmdq_pkt *pkt)
{ for (unsigned int i=0;i<pkt->count;i++) queue(NULL,pkt->ops[i].val,regs,pkt->ops[i].off,pkt->ops[i].mask); pkt->count=0; }
static void fill(u32 seed) { for (unsigned int i=0;i<ARRAY_SIZE(regs);i++) regs[i]=seed; writes=0; }
'''

MAIN = r'''
static const struct { u32 code; u8 cpp; bool alpha; } formats[]={
    {DRM_FORMAT_XRGB8888,4,false},{DRM_FORMAT_ARGB8888,4,true},
    {DRM_FORMAT_BGRX8888,4,false},{DRM_FORMAT_BGRA8888,4,true},
    {DRM_FORMAT_ABGR8888,4,true},{DRM_FORMAT_XBGR8888,4,false},
    {DRM_FORMAT_RGB888,3,false},{DRM_FORMAT_BGR888,3,false},{DRM_FORMAT_RGB565,2,false}
};
static void exhaustive(void)
{
    struct mtk_disp_ovl ovl={.data=&mt6765_ovl_driver_data,.regs=regs};
    struct device dev={.data=&ovl};
    struct drm_format_info info={0}; struct drm_framebuffer fb={.format=&info};
    struct mtk_plane_state state={.base={.fb=&fb},.pending={.enable=true,.width=13,.height=7,.pitch=256,.addr=0x58000000}};
    /* All opacity values must be monotonic, including RGB without pixel alpha. */
    for (unsigned int f=0;f<ARRAY_SIZE(formats);f++) {
        state.pending.format=formats[f].code; info.cpp[0]=formats[f].cpp; info.has_alpha=formats[f].alpha;
        for (unsigned int mode=0;mode<3;mode++) {
            state.base.pixel_blend_mode=mode;
            for (unsigned int alpha=0;alpha<=65535;alpha++) {
                state.base.alpha=alpha; mtk_ovl_layer_config(&dev,0,&state,NULL);
                assert((regs[0x30/4]&255)==alpha/256 && (regs[0x30/4]&256));
                assert(!!(regs[0x44/4]&(1U<<28))==(mode==2 || !info.has_alpha));
            }
        }
        /* The first byte fetched must lie at the reflected source-rectangle corner.
         * Use padded rows, crops and every rotate-180/reflection composition. */
        for (unsigned int padding=0;padding<2;padding++) {
            unsigned int pitch=64*info.cpp[0]+padding*37;
            state.pending.pitch=pitch;
            for (unsigned int x=0;x<3;x++) for (unsigned int y=0;y<3;y++) {
                uint64_t base=0x58000000,origin=base+y*pitch+x*info.cpp[0];
                for (unsigned int r=0;r<8;r++) {
                    unsigned int flipx=!!(r&1)^!!(r&4),flipy=!!(r&2)^!!(r&4);
                    state.pending.rotation=(r&4?DRM_MODE_ROTATE_180:DRM_MODE_ROTATE_0)|
                        (r&1?DRM_MODE_REFLECT_X:0)|(r&2?DRM_MODE_REFLECT_Y:0);
                    state.pending.addr=origin;
                    struct cmdq_pkt pkt={0}; fill(0xa5a55a5a);
                    mtk_ovl_layer_config(&dev,0,&state,&pkt); assert(!writes); flush(&pkt);
                    uint64_t expected=base+(y+(flipy?6:0))*pitch+
                        (x+(flipx?13:0))*info.cpp[0]-(flipx?1:0);
                    assert(regs[0xf40/4]==expected);
                    assert(!!(regs[0x30/4]&BIT(10))==flipx && !!(regs[0x30/4]&BIT(9))==flipy);
                    assert(!regs[0x4c/4] && !regs[0x34/4] && !regs[0xfc0/4] && regs[0x25c/4]==0xff000000);
                }
            }
        }
    }
    /* Disable has no framebuffer dependency and only removes the selected layer. */
    state.base.fb=NULL; state.pending.enable=false; fill(~0U);
    mtk_ovl_layer_config(&dev,2,&state,NULL);
    assert(writes==2 && regs[0x2c/4]==(~0U&~BIT(2)) && !regs[0x100/4]);
    /* Preserve the pre-existing MT8192 plane behavior byte for byte. */
    ovl.data=&mt8192_ovl_driver_data; state.base.fb=&fb; info.has_alpha=false; info.cpp[0]=4;
    state.pending=(struct mtk_plane_pending_state){.enable=true,.format=DRM_FORMAT_XRGB8888,
        .addr=0x58000000,.width=13,.height=7,.pitch=256,.rotation=DRM_MODE_REFLECT_X};
    state.base.alpha=0x1234; state.base.pixel_blend_mode=0; fill(0xa5a55a5a);
    mtk_ovl_layer_config(&dev,0,&state,NULL);
    assert(regs[0x30/4]==0x803434 && regs[0xf40/4]==0x580000ff && writes==8);
    assert(regs[0x4c/4]==0xa5a55a5a && regs[0xfc0/4]==0xa5a55a5a);
}
int main(int argc,char **argv)
{
    if (argc==1) { exhaustive(); puts("PASS exhaustive"); return 0; }
    assert(argc==14);
    unsigned int v[13]; for (unsigned int i=0;i<13;i++) v[i]=strtoul(argv[i+1],NULL,0);
    unsigned int module=v[0],layer=v[1],f=v[2],mode=v[3],alpha=v[4],seed=v[5];
    assert(module<2 && f<ARRAY_SIZE(formats) && mode<3 && alpha<=65535);
    struct mtk_disp_ovl ovl={.data=module?&mt6765_ovl_2l_driver_data:&mt6765_ovl_driver_data,.regs=regs};
    assert(layer<ovl.data->layer_nr);
    struct device dev={.data=&ovl};
    struct drm_format_info info={.cpp={formats[f].cpp},.has_alpha=formats[f].alpha};
    struct drm_framebuffer fb={.format=&info};
    struct mtk_plane_state state={.base={.fb=&fb,.alpha=alpha,.pixel_blend_mode=mode},
        .pending={.enable=true,.format=formats[f].code,.addr=0x58000000+v[7]*v[10]+v[6]*info.cpp[0],
            .pitch=v[10],.width=v[8],.height=v[9],.x=5,.y=9,.rotation=v[11]}};
    fill(seed);trace=true;struct cmdq_pkt pkt={0};
    mtk_ovl_layer_config(&dev,layer,&state,v[12]?&pkt:NULL);
    if (v[12]) { assert(!writes); flush(&pkt); }
    return 0;
}
'''


def harness():
    source = args.source.read_text()
    result = PRELUDE
    # Use the real DRM ABI constants instead of inventing sequential enum values.
    for path in ['include/uapi/drm/drm_fourcc.h', 'include/uapi/drm/drm_mode.h', 'include/drm/drm_blend.h']:
        text = (SRC/path).read_text()
        for name in sorted(set(re.findall(r'\bDRM_(?:FORMAT_\w+|MODE_(?:BLEND|ROTATE|REFLECT)_\w+)\b',source+MAIN))):
            if name == 'DRM_FORMAT_MOD_LINEAR': continue
            found = re.search(r'^#define\s+'+name+r'\s+[^\n]+',text,re.M)
            if found: result += found[0]+'\n'
    result += source[source.index('#define DISP_REG_OVL_INTEN'):source.index('static inline bool is_10bit_rgb')]
    for name in ['static inline bool is_10bit_rgb','static const u32 mt8173_formats',
            'struct mtk_disp_ovl_data {','struct mtk_disp_ovl {']:
        result += block(source,name)
    for name in ['mtk_ovl_set_afbc','mtk_ovl_set_bit_depth','mtk_ovl_layer_on','mtk_ovl_layer_off',
            'mtk_ovl_fmt_convert','mtk_ovl_afbc_layer_config','mtk_ovl_layer_config']:
        match = re.search(r'^(?:static )?(?:unsigned int|void) '+name+r'\(',source,re.M); assert match,name
        result += block(source,match[0])
    for name in ['mt6765_ovl','mt6765_ovl_2l','mt8192_ovl']:
        result += block(source,'static const struct mtk_disp_ovl_data '+name+'_driver_data')
    return result+MAIN


def check():
    raw = (ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest() == '71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    folder=ROOT/'out/ovl-plane-test'; folder.mkdir(exist_ok=True)
    source=folder/'harness.c'; binary=folder/'harness'; source.write_text(harness())
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror',
        '-fsanitize=address,undefined','-fno-pie','-no-pie',str(source),'-o',str(binary)],check=True)
    audit=[]
    for module in [0,1]:
        for layer in range(2 if module else 4):
            for f,(name,cpp,has_alpha,coverage,premulti) in enumerate(FORMATS):
                for mode in range(3):
                    # Vary opacity, dirty inherited state, crop, padding and queue transport.
                    alpha=[0,0x1ff,0x7fff,0x8000,0xff00,0xffff][(layer+f+mode)%6]
                    seed=[0,0xffffffff,0xa5a55a5a][(f+mode)%3]
                    x,y,width,height,pitch=3+f,2+layer,13+mode,7+layer,1024+4*f
                    fmt=premulti if mode==0 and has_alpha else coverage
                    old=stock(raw,module,layer,fmt,alpha//256,int(mode==2 or not has_alpha),seed,x,y,width,height,pitch)
                    argv=[module,layer,f,mode,alpha,seed,x,y,width,height,pitch,1,(f+layer+mode)%2]
                    result=subprocess.check_output([str(binary),*map(str,argv)],text=True)
                    new=[tuple(int(word,16) for word in line.split()[1:]) for line in result.splitlines()]
                    # Stock config enables RDMA before its fields. Native enables SRC last
                    # and reapplies validated GMC settings; compare per-plane register values.
                    expected=dict(old); actual=dict(new)
                    assert actual.pop(0xc8+0x20*layer)==0x03ff03ff
                    assert actual.pop(0x2c)==seed|(1<<layer)
                    assert actual==expected,(name,module,layer,mode,expected,actual)
                    assert new[-1][0]==0x2c
                    audit.append({'module':module,'layer':layer,'format':name,'blend':mode,'alpha':alpha,
                        'seed':seed,'crop':[x,y,width,height],'pitch':pitch,'stock':old,'native':new})
    subprocess.run([str(binary)],check=True)
    (ROOT/'out/mt6765-ovl-plane-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(f'PASS: {len(audit)} RGB plane fixtures match shipped ovl_layer_config register values')
    print('PASS: all 65,536 opacity values for nine formats and three blend modes; 1,296 cropped reflection cases')
    print('PASS: queued writes, stale RGB clipping, disable without framebuffer and unchanged MT8192 plane setup')
    print('No scanout or blending pixels are emulated. Stock rotation is compiled out; reflection uses a DRM rectangle-address oracle.')


if __name__ == '__main__': check()
