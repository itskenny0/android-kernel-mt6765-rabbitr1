/* SPDX-License-Identifier: GPL-2.0-only */

#include <assert.h>
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
typedef uint8_t u8; typedef uint16_t u16; typedef uint32_t u32;
#define BIT(x) (1U << (x))
#define GENMASK(h,l) ((~0U << (l)) & (~0U >> (31-(h))))
#define ARRAY_SIZE(x) (sizeof(x)/sizeof((x)[0]))
#include "mt6765-pm-domains.h"
struct event { int write; u32 offset, value; } events[64], expected[64];
static unsigned nevents;
struct regmap { u32 status; int read_error, write_error; u32 suppress; unsigned bad; } infra, wrong;
struct scpsys { u8 bus_prot_index[BUS_PROT_BLOCK_COUNT]; struct regmap **bus_prot; };
struct scpsys_domain { const struct scpsys_domain_data *data; struct scpsys *scpsys; };
static void event(int wr, u32 off, u32 val) {
 assert(nevents<ARRAY_SIZE(events)); events[nevents++]=(struct event){wr,off,val};
}
static int regmap_write(struct regmap *m,u32 off,u32 val) {
 assert(m==&infra); event(1,off,val); if (m->write_error) return m->write_error;
 if(off==0x2a0) m->status|=val & ~m->suppress;
 else {assert(off==0x2a4);m->status&=~val;} return 0;
}
static int regmap_read(struct regmap *m,u32 off,u32 *val) {
 assert(m==&infra && off==0x228); *val=m->status;event(0,off,*val);return m->read_error;
}
#define regmap_clear_bits(m,o,v) regmap_write(m,o,v)
#define regmap_set_bits(m,o,v) regmap_write(m,o,v)
#define MTK_POLL_DELAY_US 10
#define MTK_POLL_TIMEOUT 1000000
/* Bounded poll-time model, not elapsed hardware time. */
#define regmap_read_poll_timeout(m,o,v,cond,delay,timeout) ({ \
 int rr=-ETIMEDOUT; for(unsigned n=0;n<3;n++) { \
 int r=regmap_read(m,o,&v);if(r){rr=r;break;}if(cond){rr=0;break;} } rr; })
static u32 sram;
#define INFRACFG_REG(o) (o)
#define SPM_REG(o) (0x10000+(o))
static void spm_write(u32 off,u32 val) {
 if(off<0x10000) {assert(regmap_write(&infra,off,val)==0);return;}
 assert(off==0x10338);sram=val;if(val & BIT(8))sram|=BIT(12);else sram&=~BIT(12);
}
static u32 spm_read(u32 off) {
 u32 v;if(off<0x10000){assert(regmap_read(&infra,off,&v)==0);return v;}
 assert(off==0x10338);return sram;
}
static int DBG_ID,DBG_STA,DBG_STEP;
#define DBG_ID_MFG_BUS 1
#define INCREASE_STEPS (++DBG_STEP)
static void ram_console_update(void) {}

/* @BUS_HELPERS@ */
#include "mt6765-mfg-stock-oracle.h"

static void reset(void){nevents=0;memset(events,0,sizeof(events));memset(&infra,0,sizeof(infra));sram=0;}
int main(void){
 struct regmap *maps[]={&wrong,&infra};struct scpsys sc={.bus_prot=maps};
 sc.bus_prot_index[BUS_PROT_BLOCK_INFRA]=1;
 struct scpsys_domain pd={.data=&scpsys_domain_data_mt6765[MT6765_POWER_DOMAIN_MFG],.scpsys=&sc};
 unsigned n;int cases=0;
 assert(mt6765_scpsys_data.num_bus_prot_blocks==1);
 assert(mt6765_scpsys_data.bus_prot_blocks[0]==BUS_PROT_BLOCK_INFRA);
 reset();assert(spm_mtcmos_ctrl_mfg_bus_prot(0)==0);n=nevents;memcpy(expected,events,sizeof(events));
 reset();assert(scpsys_bus_protect_enable(&pd,0)==0);assert(n==nevents && memcmp(expected,events,n*sizeof(*events))==0);cases++;
 reset();infra.status=0x2600000;assert(spm_mtcmos_ctrl_mfg_bus_prot(1)==0);n=nevents;memcpy(expected,events,sizeof(events));
 reset();infra.status=0x2600000;assert(scpsys_bus_protect_disable(&pd,0)==0);assert(n==2 && n==nevents && memcmp(expected,events,n*sizeof(*events))==0);cases++;
 reset();assert(scpsys_bus_protect_enable(&pd,BUS_PROT_IGNORE_SUBCLK)==0 && nevents==0);
 assert(scpsys_bus_protect_disable(&pd,BUS_PROT_IGNORE_SUBCLK)==0 && nevents==0);cases++;
 for(unsigned i=0;i<ARRAY_SIZE(scpsys_domain_data_mt6765);i++){
  if(i==MT6765_POWER_DOMAIN_MFG)continue;
  pd.data=&scpsys_domain_data_mt6765[i];reset();assert(scpsys_bus_protect_enable(&pd,0)==0);assert(scpsys_bus_protect_disable(&pd,0)==0);assert(nevents==0);
 }cases++;
 pd.data=&scpsys_domain_data_mt6765[MT6765_POWER_DOMAIN_MFG];
 reset();infra.suppress=BIT(25);assert(scpsys_bus_protect_enable(&pd,0)==-ETIMEDOUT);assert(nevents==4);assert(events[0].value==BIT(25));cases++;
 reset();infra.suppress=BIT(21)|BIT(22);assert(scpsys_bus_protect_enable(&pd,0)==-ETIMEDOUT);assert(nevents==6);assert(events[2].value==(BIT(21)|BIT(22)));cases++;
 reset();infra.read_error=-EIO;assert(scpsys_bus_protect_enable(&pd,0)==-EIO);assert(nevents==2);cases++;
 /* Failed clear must return its error even when ACK is intentionally skipped. */
 reset();infra.status=0x2600000;infra.write_error=-EIO;assert(scpsys_bus_protect_disable(&pd,0)==-EIO);assert(nevents==1 && infra.status==0x2600000);cases++;
 /* A stale asserted ACK must not conceal a failed set command. */
 reset();infra.status=0x2600000;infra.write_error=-EIO;assert(scpsys_bus_protect_enable(&pd,0)==-EIO);assert(nevents==1);cases++;
 printf("{\"passed\":true,\"controls\":%d,\"scope\":\"actual source helpers/table versus actual stock routine with modeled register IO\",\"generic_write_error_regressions\":2}\n",cases);
 return 0;
}
