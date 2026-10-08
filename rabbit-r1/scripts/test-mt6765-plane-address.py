#!/usr/bin/env python3
"""Exercise real pending-state/OVL callbacks and compare packed-YUV crops to stock."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT=Path('/rabbitr1');SRC=ROOT/'src/mainline'
parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--plane-source',type=Path,default=SRC/'drivers/gpu/drm/mediatek/mtk_plane.c')
parser.add_argument('--ovl-source',type=Path,default=SRC/'drivers/gpu/drm/mediatek/mtk_disp_ovl.c')
args=parser.parse_args()
for path in vars(args).values():
    if not path.resolve().is_relative_to(ROOT):raise SystemExit('Sources must be under /rabbitr1')
spec=importlib.util.spec_from_file_location('rgb',Path(__file__).with_name('test-mt6765-ovl-plane.py'))
rgb=importlib.util.module_from_spec(spec)
argv=sys.argv;sys.argv=[argv[0]];spec.loader.exec_module(rgb);sys.argv=argv
rgb.args.source=args.ovl_source

EXTRA=r'''
#include <errno.h>
#include <stddef.h>
#include <string.h>
typedef uint64_t u64;
#define U16_MAX UINT16_MAX
#define U32_MAX UINT32_MAX
#define ALIGN(x,a) (((x)+(a)-1)&~((a)-1))
#define DRM_PLANE_NO_SCALING 65536
#define IS_ERR(p) ((uintptr_t)(p)>=(uintptr_t)-4095)
#define PTR_ERR(p) ((intptr_t)(p))
#define WARN_ON(x) (x)
#define wmb() ((void)0)
#define swap(a,b) do { __typeof__(a) tmp=(a); (a)=(b); (b)=tmp; } while (0)
enum drm_color_encoding { DRM_COLOR_YCBCR_BT601 };
struct drm_rect { int x1,y1,x2,y2; };
static int drm_rect_width(const struct drm_rect *r) { return r->x2-r->x1; }
static int drm_rect_height(const struct drm_rect *r) { return r->y2-r->y1; }
struct drm_gem_object { uint64_t size; };
struct drm_gem_dma_object { struct drm_gem_object base; dma_addr_t dma_addr; };
static struct drm_gem_dma_object *to_drm_gem_dma_obj(struct drm_gem_object *obj)
{ return (struct drm_gem_dma_object *)obj; }
struct drm_plane;
struct drm_crtc { struct drm_plane *cursor; };
struct drm_crtc_state { int unused; };
'''
FRAMEBUFFER=r'''
struct drm_framebuffer {
    const struct drm_format_info *format; struct drm_gem_object *obj[4];
    unsigned int pitches[4],offsets[4],width,height; uint64_t modifier;
};
'''
PLANE_STATE=r'''
struct drm_plane_state {
    struct drm_framebuffer *fb; unsigned int alpha,pixel_blend_mode;
    struct drm_crtc *crtc; unsigned int rotation,src_x,src_y,src_w,src_h;
    int crtc_x,crtc_y; unsigned int crtc_w,crtc_h; struct drm_rect src,dst;
    bool visible; enum drm_color_encoding color_encoding;
};
'''
STUBS=r'''
struct drm_plane { struct drm_plane_state *state; };
struct drm_atomic_state { struct drm_plane_state *new_state; };
static struct drm_crtc_state crtc_state;
static struct device *checked_dev;
static unsigned int local_layer,clip_calls,check_calls,async_calls;
static int clip_error,get_error;
static bool clip_visible,missing_crtc;
static struct drm_rect clipped_src,clipped_dst;
static struct drm_plane_state *expected_state;
static struct mtk_plane_state *to_mtk_plane_state(struct drm_plane_state *state)
{ return (struct mtk_plane_state *)state; }
static struct drm_plane_state *drm_atomic_get_new_plane_state(struct drm_atomic_state *state,struct drm_plane *plane)
{ (void)plane; return state->new_state; }
static struct drm_crtc_state *drm_atomic_get_crtc_state(struct drm_atomic_state *state,struct drm_crtc *crtc)
{ (void)state; assert(crtc); return get_error?(void *)(intptr_t)get_error:&crtc_state; }
static struct drm_crtc_state *drm_atomic_get_new_crtc_state(struct drm_atomic_state *state,struct drm_crtc *crtc)
{ (void)state; assert(crtc); return missing_crtc?NULL:&crtc_state; }
/* DRM core clipping is modeled; the production callers must pass the new state,
 * propagate errors and run hardware validation only after these rectangles exist. */
static int drm_atomic_helper_check_plane_state(struct drm_plane_state *state,struct drm_crtc_state *crtc,
    int min_scale,int max_scale,bool position,bool disabled)
{
    assert(state==expected_state && crtc==&crtc_state && min_scale==65536 && max_scale==65536 && position && disabled);
    clip_calls++; if (clip_error) return clip_error;
    state->src=clipped_src; state->dst=clipped_dst; state->visible=clip_visible; return 0;
}
static int mtk_ovl_layer_check(struct device *,unsigned int,struct mtk_plane_state *);
static int mtk_crtc_plane_check(struct drm_crtc *crtc,struct drm_plane *plane,struct mtk_plane_state *state)
{
    (void)plane; assert(crtc && clip_calls && &state->base==expected_state); check_calls++;
    return mtk_ovl_layer_check(checked_dev,local_layer,state);
}
static void mtk_crtc_async_update(struct drm_crtc *crtc,struct drm_plane *plane,struct drm_atomic_state *state)
{ assert(crtc && plane->state && state->new_state); async_calls++; }
'''

MAIN=r'''
static struct drm_gem_dma_object object={.base={.size=0x100000},.dma_addr=0x58000000};
static struct drm_format_info info;
static struct drm_framebuffer fb;
static struct mtk_plane_state state;
static struct mtk_disp_ovl ovl;
static struct device dev;
static void setup(unsigned int module,unsigned int format,unsigned int cpp,bool yuv,
                  unsigned int x,unsigned int y,unsigned int w,unsigned int h,unsigned int pitch,unsigned int offset)
{
    info=(struct drm_format_info){.format=format,.cpp={cpp},.is_yuv=yuv};
    fb=(struct drm_framebuffer){.format=&info,.obj={&object.base},.pitches={pitch},.offsets={offset},.width=128,.height=64};
    state=(struct mtk_plane_state){.base={.fb=&fb,.alpha=0x8000,.pixel_blend_mode=DRM_MODE_BLEND_COVERAGE,
        .visible=true,.rotation=DRM_MODE_ROTATE_0,.src={x<<16,y<<16,(x+w)<<16,(y+h)<<16},.dst={5,9,5+w,9+h}}};
    ovl=(struct mtk_disp_ovl){.data=module?&mt6765_ovl_2l_driver_data:&mt6765_ovl_driver_data,.regs=regs};
    dev=(struct device){.data=&ovl};checked_dev=&dev;
    object=(struct drm_gem_dma_object){.base={.size=0x100000},.dma_addr=0x58000000};
}
static void checks(void)
{
    setup(0,DRM_FORMAT_UYVY,2,true,3,7,13,6,512,4096);
    assert(!mtk_ovl_layer_check(&dev,0,&state));
    uint64_t end=4096+12*512+16*2;
    object.base.size=end;assert(!mtk_ovl_layer_check(&dev,0,&state));
    object.base.size=end-1;assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    object.base.size=end;object.dma_addr=UINT32_MAX-end+1;assert(!mtk_ovl_layer_check(&dev,0,&state));
    object.dma_addr++;assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    object.dma_addr=1ULL<<32;assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    object.dma_addr=UINT64_MAX;assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    object.dma_addr=0x58000000;object.base.size=0x100000;
    assert(mtk_ovl_layer_check(&dev,4,&state)==-EINVAL);
    ovl.data=&mt6765_ovl_2l_driver_data;assert(mtk_ovl_layer_check(&dev,2,&state)==-EINVAL);
    ovl.data=&mt6765_ovl_driver_data;
    for (unsigned int n=0;n<20;n++) {
        struct mtk_plane_state old=state;struct drm_framebuffer oldfb=fb;
        switch (n) {
        case 0:fb.pitches[0]=65536;break;
        case 1:fb.pitches[0]=0;break;
        case 2:fb.pitches[0]=31;break; /* End of chroma pair requires 32 bytes. */
        case 3:fb.modifier=1;break;
        case 4:state.base.src.x1++;state.base.src.x2++;break;
        case 5:state.base.src.y1++;break;
        case 6:state.base.src.x2++;break;
        case 7:state.base.src.y2++;break;
        case 8:state.base.src.x2=state.base.src.x1;break;
        case 9:state.base.src.y2=state.base.src.y1;break;
        case 10: /* The visible width fits; its expanded fetch is 4096 pixels. */
            state.base.src.x2=state.base.src.x1+(4095<<16);fb.pitches[0]=65535;
            state.base.dst=(struct drm_rect){0,0,4095,6};break;
        case 11:state.base.src.y2=state.base.src.y1+(4096<<16);break;
        case 12:state.base.dst.x1=-1;break;
        case 13:state.base.dst.y1=-1;break;
        case 14:state.base.dst.x2=4096;break;
        case 15:state.base.dst.y2=4096;break;
        case 16:state.base.src.x1=-65536;break;
        case 17:fb.offsets[0]=UINT32_MAX;break;
        case 18:state.base.rotation=DRM_MODE_REFLECT_X;break;
        case 19:state.base.rotation=DRM_MODE_ROTATE_90;break;
        }
        assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
        state=old;fb=oldfb;
    }
    /* Pair expansion at an odd framebuffer edge needs allocation and row padding. */
    setup(0,DRM_FORMAT_YUYV,2,true,12,0,1,1,26,0);fb.width=13;
    assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    fb.pitches[0]=28;object.base.size=27;assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    object.base.size=28;assert(!mtk_ovl_layer_check(&dev,0,&state));
    setup(0,DRM_FORMAT_UYVY,2,true,1,0,4093,1,8192,0);fb.width=4096;state.base.dst=(struct drm_rect){0,0,4093,1};
    assert(!mtk_ovl_layer_check(&dev,0,&state));
    state.base.src.x2+=1<<16;assert(mtk_ovl_layer_check(&dev,0,&state)==-EINVAL);
    setup(0,DRM_FORMAT_XRGB8888,4,false,0,0,4095,1,65535,0);fb.width=4095;state.base.dst=(struct drm_rect){0,0,4095,1};
    assert(!mtk_ovl_layer_check(&dev,0,&state));
    /* Native constraints do not change MT8192 validation. */
    ovl.data=&mt8192_ovl_driver_data;fb.pitches[0]=65536;fb.offsets[0]=UINT32_MAX;
    assert(!mtk_ovl_layer_check(&dev,0,&state));
}
static void address(void)
{
    unsigned int formats[]={DRM_FORMAT_RGB565,DRM_FORMAT_RGB888,DRM_FORMAT_XRGB8888};
    for (unsigned int f=0;f<3;f++) for (unsigned int offset=0;offset<3;offset++)
        for (unsigned int crop=0;crop<3;crop++) for (unsigned int rotation=0;rotation<8;rotation++) {
            unsigned int cpp=f+2,x=crop*5,y=crop*7,w=13,h=6,pitch=512+f*16;
            setup(0,formats[f],cpp,false,x,y,w,h,pitch,offset*4096+offset*3);
            state.base.rotation=(rotation&4?DRM_MODE_ROTATE_180:DRM_MODE_ROTATE_0)|
                (rotation&1?DRM_MODE_REFLECT_X:0)|(rotation&2?DRM_MODE_REFLECT_Y:0);
            assert(!mtk_ovl_layer_check(&dev,0,&state));
            mtk_plane_update_new_state(&state.base,&state);
            assert(state.pending.addr==0x58000000+fb.offsets[0]+y*pitch+x*cpp && state.pending.src_x==x);
            struct cmdq_pkt pkt={0};fill(~0U);mtk_ovl_layer_config(&dev,0,&state,&pkt);assert(!writes);flush(&pkt);
            bool rx=!!(rotation&1)^!!(rotation&4),ry=!!(rotation&2)^!!(rotation&4);
            assert(regs[0xf40/4]==0x58000000+fb.offsets[0]+(y+(ry?h-1:0))*pitch+(x+(rx?w:0))*cpp-(rx?1:0));
        }
    /* Generic linear address math must not sign-extend offsets above INT_MAX. */
    setup(0,DRM_FORMAT_RGB565,2,false,0,2000,1,1,1200000,4096);
    mtk_plane_update_new_state(&state.base,&state);
    assert(state.pending.addr==0x58000000ULL+4096+2400000000ULL);
    /* The dormant AFBC callback also includes the framebuffer offset in both planes. */
    setup(0,DRM_FORMAT_XRGB8888,4,false,32,8,32,8,512,4096);fb.modifier=1;fb.width=128;fb.height=64;
    mtk_plane_update_new_state(&state.base,&state);
    assert(state.pending.hdr_addr==0x58000000+4096+64+16 && state.pending.hdr_pitch==64);
    assert(state.pending.addr==0x58000000+4096+1024+4096+2048 && state.pending.pitch==4096);
    assert(state.pending.modifier==1);
}
static void atomic(void)
{
    setup(0,DRM_FORMAT_UYVY,2,true,0,0,13,6,512,4096);
    struct mtk_plane_state old=state;
    struct drm_framebuffer old_fb=fb;old_fb.offsets[0]=8192;old.base.fb=&old_fb;
    struct drm_plane plane={.state=&old.base};struct drm_crtc crtc={.cursor=&plane};
    state.base.crtc=&crtc;old.base.crtc=&crtc;struct drm_atomic_state transaction={.new_state=&state.base};
    expected_state=&state.base;clip_visible=true;
    clipped_src=(struct drm_rect){3<<16,7<<16,16<<16,13<<16};clipped_dst=(struct drm_rect){0,0,13,6};
    fb.width=4096;state.base.src=(struct drm_rect){0,0,4096<<16,6<<16}; /* Only the clipped geometry fits. */
    clip_calls=check_calls=0;assert(!mtk_plane_atomic_check(&plane,&transaction));
    assert(clip_calls==1 && check_calls==1);
    clip_calls=check_calls=0;clip_error=-ERANGE;
    assert(mtk_plane_atomic_check(&plane,&transaction)==-ERANGE && !check_calls);
    clip_error=0;get_error=-ENOMEM;
    assert(mtk_plane_atomic_check(&plane,&transaction)==-ENOMEM);get_error=0;
    /* Async validation clips the NEW state; the previous rectangles remain intact. */
    struct drm_rect before=old.base.src;clip_calls=check_calls=0;
    assert(!mtk_plane_atomic_async_check(&plane,&transaction,false));
    assert(clip_calls==1 && check_calls==1 && !memcmp(&before,&old.base.src,sizeof(before)));
    mtk_plane_atomic_async_update(&plane,&transaction);
    assert(old.base.fb==&fb && state.base.fb==&old_fb);
    assert(async_calls==1 && old.pending.async_dirty && old.pending.src_x==3);
    assert(old.pending.addr==0x58000000+4096+7*512+6 && old.base.visible);
    assert(!memcmp(&old.base.src,&clipped_src,sizeof(clipped_src)) && !memcmp(&old.base.dst,&clipped_dst,sizeof(clipped_dst)));
    fill(~0U);mtk_ovl_layer_config(&dev,0,&old,NULL);
    assert(regs[0x4c/4]==1 && regs[0x38/4]==(6U<<16|14) && regs[0xf40/4]==0x58000000+4096+7*512+4);
    /* Off-screen async moves disable the layer instead of programming a zero-size fetch. */
    clip_visible=false;clipped_src=(struct drm_rect){0};clipped_dst=(struct drm_rect){0};
    assert(!mtk_plane_atomic_async_check(&plane,&transaction,false));
    mtk_plane_atomic_async_update(&plane,&transaction);
    assert(!old.pending.enable && !old.base.visible);fill(~0U);mtk_ovl_layer_config(&dev,0,&old,NULL);
    assert(writes==2 && !(regs[0x2c/4]&1) && !regs[0xc0/4]);
    missing_crtc=true;assert(mtk_plane_atomic_async_check(&plane,&transaction,false)==-EINVAL);missing_crtc=false;
    state.base.crtc=NULL;assert(mtk_plane_atomic_async_check(&plane,&transaction,false)==-EINVAL);state.base.crtc=&crtc;
    state.base.fb=NULL;assert(mtk_plane_atomic_async_check(&plane,&transaction,false)==-EINVAL);
    assert(!mtk_plane_atomic_check(&plane,&transaction));
}
int main(int argc,char **argv)
{
    if (argc==1) {checks();address();atomic();puts("PASS address, bounds and atomic state");return 0;}
    assert(argc==9);unsigned int v[8];for(unsigned int i=0;i<8;i++)v[i]=strtoul(argv[i+1],NULL,0);
    unsigned int module=v[0],layer=v[1],fmt=v[2],x=v[3],w=v[4],seed=v[5],offset=v[6],queued=v[7];
    setup(module,fmt?DRM_FORMAT_YUYV:DRM_FORMAT_UYVY,2,true,x,7,w,6,512,offset);
    assert(!mtk_ovl_layer_check(&dev,layer,&state));mtk_plane_update_new_state(&state.base,&state);
    fill(seed);trace=true;struct cmdq_pkt pkt={0};mtk_ovl_layer_config(&dev,layer,&state,queued?&pkt:NULL);
    if(queued){assert(!writes);flush(&pkt);}return 0;
}
'''


def harness():
    text=rgb.harness().removesuffix(rgb.MAIN)
    text+=re.search(r'^#define\s+DRM_MODE_ROTATE_90\s+[^\n]+',
        (SRC/'include/uapi/drm/drm_mode.h').read_text(),re.M)[0]+'\n'
    text=text.replace('typedef uint64_t dma_addr_t;', 'typedef uint64_t dma_addr_t;\n'+EXTRA)
    text=text.replace('struct drm_format_info { bool', 'struct drm_format_info { u32 format; bool')
    text=text.replace('struct drm_framebuffer { const struct drm_format_info *format; };',FRAMEBUFFER)
    text=text.replace('struct drm_plane_state { struct drm_framebuffer *fb; unsigned int alpha,pixel_blend_mode; };',PLANE_STATE)
    header=(SRC/'drivers/gpu/drm/mediatek/mtk_plane.h').read_text()
    text=re.sub(r'struct mtk_plane_pending_state \{.*?\n};',rgb.block(header,'struct mtk_plane_pending_state {').rstrip(),text,flags=re.S)
    text+=STUBS
    source=args.ovl_source.read_text()
    for start in ['unsigned int mtk_ovl_supported_rotations','static int mt6765_ovl_plane_check','int mtk_ovl_layer_check']:
        piece=rgb.block(source,start)
        if start=='int mtk_ovl_layer_check':piece='static '+piece
        text+=piece
    for line in header.splitlines():
        if line.startswith('#define AFBC_'):text+=line+'\n'
    source=args.plane_source.read_text()
    for start in ['static int mtk_plane_atomic_async_check','static void mtk_plane_update_new_state',
                  'static void mtk_plane_atomic_async_update','static int mtk_plane_atomic_check']:
        text+=rgb.block(source,start)
    return text+MAIN


def check():
    raw=(ROOT/'firmware/stock-v0.8.293/boot-unpacked/Image').read_bytes()
    assert hashlib.sha256(raw).hexdigest()=='71d9fd10bbf39272add38e94b993d17b81948e4978791e51bdaeb3f2736a431f'
    folder=ROOT/'out/plane-address-test';folder.mkdir(exist_ok=True)
    source=folder/'harness.c';binary=folder/'harness';source.write_text(harness())
    subprocess.run(['cc','-std=gnu11','-O1','-g','-Wall','-Wextra','-Werror','-Wno-unused-parameter',
        '-fsanitize=address,undefined','-fno-pie','-no-pie',str(source),'-o',str(binary)],check=True)
    subprocess.run([str(binary)],check=True)
    audit=[]
    for module in [0,1]:
        for layer in range(2 if module else 4):
            for fmt,ufmt in enumerate([0x201012,0x201414]):
                for x,w in [(2,12),(3,12),(2,13),(3,13)]:
                    for seed in [0,0xffffffff,0xa5a55a5a]:
                        offset=4096+layer*8+fmt*4
                        old=rgb.stock(raw,module,layer,ufmt,128,1,seed,x,7,w,6,512,0x58000000+offset)
                        result=subprocess.check_output([str(binary),*map(str,[module,layer,fmt,x,w,seed,offset,seed&1])],text=True)
                        new=[tuple(int(word,16) for word in line.split()[1:]) for line in result.splitlines()]
                        expected=dict(old);actual=dict(new)
                        assert actual.pop(0xc8+0x20*layer)==0x03ff03ff
                        assert actual.pop(0x2c)==seed|(1<<layer)
                        assert actual==expected,(module,layer,fmt,x,w,expected,actual)
                        audit.append({'module':module,'layer':layer,'format':['UYVY','YUYV'][fmt],
                            'x':x,'width':w,'fb_offset':offset,'seed':seed,'stock':old,'native':new})
    (ROOT/'out/mt6765-plane-address-audit.json').write_text(json.dumps(audit,indent=2)+'\n')
    print(f'PASS: {len(audit)} packed-YUV register fixtures match shipped instructions through the production pending-address callback')
    print('PASS: framebuffer offsets, 216 cropped RGB reflection cases, wide offsets, AFBC address bookkeeping, bounds and allocation ends')
    print('PASS: normal/async validation order, new-state clipping, error propagation, async visibility, chroma pairs and legacy checks')
    print('DRM clipping and kernel services are modeled. No DMA, IOMMU or displayed pixels are emulated.')

if __name__=='__main__':check()
