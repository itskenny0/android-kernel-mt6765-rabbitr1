#!/usr/bin/env python3
"""Exercise the production MediaTek DSI transfer callback with modeled hardware."""
import os
from pathlib import Path
import resource
import subprocess
import sys

ROOT = Path('/rabbitr1')
resource.setrlimit(resource.RLIMIT_CORE, (0,0))
os.environ['TMPDIR'] = str(ROOT/'.tmp')
source = ROOT/'src/mainline/drivers/gpu/drm/mediatek/mtk_dsi.c'
if len(sys.argv) == 2:
    source = Path(sys.argv[1]).resolve()
    if not source.is_relative_to(ROOT): raise SystemExit('Source must be under /rabbitr1')
s = source.read_text()
start = s.index('static ssize_t mtk_dsi_host_transfer(')
function = s[start:s.index('\n}',start)+2]
prelude = r'''
#include <assert.h>
#include <errno.h>
#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>
#include <stdio.h>
#include <string.h>
#include <sys/types.h>
typedef uint8_t u8;
typedef uint32_t u32;
#define MODE 3
#define DSI_MODE_CTRL 0x14
#define DSI_RX_DATA0 0x74
#define CMD_DONE_INT_FLAG 2
#define LPRX_RD_RDY_INT_FLAG 1
#define VM_DONE_INT_FLAG 8
#define DRM_ERROR(...) ((void)0)
#define DRM_INFO(...) ((void)0)
#define MTK_DSI_HOST_IS_READ(t) ((t)==0x04 || (t)==0x14 || (t)==0x24 || (t)==0x06)
struct mipi_dsi_host { int unused; };
struct mipi_dsi_msg { u8 type; const void *tx_buf; size_t tx_len; void *rx_buf; size_t rx_len; };
struct mtk_dsi_driver_data { bool mt6765_regs; };
static const struct mtk_dsi_driver_data legacy_data={0};
struct mtk_dsi { struct mipi_dsi_host host; u8 *regs; const struct mtk_dsi_driver_data *driver_data; };
static ssize_t mt6765_dsi_transfer(struct mtk_dsi *d, const struct mipi_dsi_msg *m) { (void)d;(void)m;assert(0);return 0; }
static u8 regs[0x100];
static struct mtk_dsi dsi = { .regs = regs, .driver_data=&legacy_data };
static int switch_error, send_error, starts, stops, restores, ready, sends, switches, reads;
static u32 readl(const u8 *p) { u32 v; memcpy(&v,p,4); return v; }
static u8 readb(const u8 *p) { reads++; return *p; }
static struct mtk_dsi *host_to_dsi(struct mipi_dsi_host *h) { assert(h==&dsi.host); return &dsi; }
static void mtk_dsi_stop(struct mtk_dsi *d) { assert(d==&dsi); stops++; }
static void mtk_dsi_start(struct mtk_dsi *d) { assert(d==&dsi); starts++; }
static void mtk_dsi_set_mode(struct mtk_dsi *d) { assert(d==&dsi); restores++; }
static void mtk_dsi_lane_ready(struct mtk_dsi *d) { assert(d==&dsi); ready++; }
static int mtk_dsi_switch_to_cmd_mode(struct mtk_dsi *d,u8 flag,u32 timeout)
{ assert(d==&dsi && flag==8 && timeout==500); switches++; return switch_error ? -ETIME : 0; }
static ssize_t mtk_dsi_host_send_cmd(struct mtk_dsi *d,const struct mipi_dsi_msg *m,u8 flag)
{ assert(d==&dsi && flag==(MTK_DSI_HOST_IS_READ(m->type)?3:2)); sends++; return send_error ? -ETIMEDOUT : 0; }
static u32 mtk_dsi_recv_cnt(u8 type,u8 *data)
{ assert(type==0x21 && data[1]==0x9c); return 1; }
'''
main = r'''
static void setup(unsigned int mode)
{
    memcpy(regs+DSI_MODE_CTRL,&mode,4);
    starts=stops=restores=ready=sends=switches=reads=0;
    switch_error=send_error=0;
}
int main(void)
{
    u8 tx[64]={0},rx[4];
    struct mipi_dsi_msg msg={ .tx_buf=tx };
    const u8 types[]={0x03,0x13,0x23,0x29,0x05,0x15,0x39};
    for (unsigned int mode=0;mode<4;mode++) {
        for (unsigned int t=0;t<sizeof(types);t++) {
            msg.type=types[t];
            for (size_t len=0;len<=sizeof(tx);len++) {
                setup(mode); msg.tx_len=len;
                assert(mtk_dsi_host_transfer(&dsi.host,&msg)==(ssize_t)len);
                assert(sends==1 && ready==1 && reads==0);
                assert(starts==!!mode && stops==!!mode && restores==!!mode && switches==!!mode);
            }
            setup(mode); send_error=-ETIMEDOUT;
            assert(mtk_dsi_host_transfer(&dsi.host,&msg)==-ETIMEDOUT);
            assert(sends==1 && ready==1 && reads==0 && restores==!!mode);
            if (mode) {
                setup(mode); switch_error=-ETIME;
                assert(mtk_dsi_host_transfer(&dsi.host,&msg)==-ETIME);
                assert(sends==0 && ready==0 && reads==0 && restores==1 && starts==1);
            }
        }
        setup(mode); msg.type=0x06; msg.tx_len=1; msg.rx_buf=rx; msg.rx_len=sizeof(rx);
        memset(rx,0xa5,sizeof(rx)); regs[DSI_RX_DATA0]=0x21; regs[DSI_RX_DATA0+1]=0x9c;
        assert(mtk_dsi_host_transfer(&dsi.host,&msg)==1);
        assert(rx[0]==0x9c && rx[1]==0xa5 && sends==1 && reads==16);
        setup(mode); send_error=-ETIMEDOUT; memset(rx,0xa5,sizeof(rx));
        assert(mtk_dsi_host_transfer(&dsi.host,&msg)==-ETIMEDOUT);
        assert(rx[0]==0xa5 && reads==0);
    }
    puts("PASS: DSI write lengths 0..64, seven packet types, all modes, failures and short-read regression");
    puts("Callback only; IRQ, MMIO, packet transmission and DSI PHY are modeled.");
}
'''
path = ROOT/'out/dsi-transfer-host.c'
path.write_text(prelude+function+main)
binary = ROOT/'out/dsi-transfer-host'
subprocess.run(['cc','-std=gnu11','-Wall','-Wextra','-Werror','-Wno-sign-compare',
                '-fsanitize=address,undefined','-fno-pie','-no-pie','-O1','-g',str(path),'-o',str(binary)],check=True)
subprocess.run([str(binary)],check=True)
