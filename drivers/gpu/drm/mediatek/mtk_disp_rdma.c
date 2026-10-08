// SPDX-License-Identifier: GPL-2.0-only
/*
 * Copyright (c) 2015 MediaTek Inc.
 */

#include <drm/drm_fourcc.h>

#include <linux/clk.h>
#include <linux/component.h>
#include <linux/iopoll.h>
#include <linux/math64.h>
#include <linux/module.h>
#include <linux/of.h>
#include <linux/overflow.h>
#include <linux/platform_device.h>
#include <linux/pm_runtime.h>
#include <linux/soc/mediatek/mtk-cmdq.h>

#include "mtk_crtc.h"
#include "mtk_ddp_comp.h"
#include "mtk_disp_drv.h"
#include "mtk_drm_drv.h"

#define DISP_REG_RDMA_INT_ENABLE		0x0000
#define DISP_REG_RDMA_INT_STATUS		0x0004
#define RDMA_TARGET_LINE_INT				BIT(5)
#define RDMA_FIFO_UNDERFLOW_INT				BIT(4)
#define RDMA_EOF_ABNORMAL_INT				BIT(3)
#define RDMA_FRAME_END_INT				BIT(2)
#define RDMA_FRAME_START_INT				BIT(1)
#define RDMA_REG_UPDATE_INT				BIT(0)
#define DISP_REG_RDMA_GLOBAL_CON		0x0010
#define RDMA_ENGINE_EN					BIT(0)
#define RDMA_MODE_MEMORY				BIT(1)
#define DISP_REG_RDMA_SIZE_CON_0		0x0014
#define RDMA_MATRIX_ENABLE				BIT(17)
#define RDMA_MATRIX_INT_MTX_SEL				GENMASK(23, 20)
#define RDMA_MATRIX_INT_MTX_BT601_to_RGB		(6 << 20)
#define DISP_REG_RDMA_SIZE_CON_1		0x0018
#define DISP_REG_RDMA_TARGET_LINE		0x001c
#define DISP_RDMA_MEM_CON			0x0024
#define MEM_MODE_INPUT_FORMAT_RGB565			(0x000 << 4)
#define MEM_MODE_INPUT_FORMAT_RGB888			(0x001 << 4)
#define MEM_MODE_INPUT_FORMAT_RGBA8888			(0x002 << 4)
#define MEM_MODE_INPUT_FORMAT_ARGB8888			(0x003 << 4)
#define MEM_MODE_INPUT_FORMAT_UYVY			(0x004 << 4)
#define MEM_MODE_INPUT_FORMAT_YUYV			(0x005 << 4)
#define MEM_MODE_INPUT_SWAP				BIT(8)
#define DISP_RDMA_MEM_SRC_PITCH			0x002c
#define DISP_RDMA_MEM_GMC_SETTING_0		0x0030
#define DISP_REG_RDMA_FIFO_CON			0x0040
#define RDMA_FIFO_UNDERFLOW_EN				BIT(31)
#define RDMA_FIFO_PSEUDO_SIZE(bytes)			(((bytes) / 16) << 16)
#define RDMA_OUTPUT_VALID_FIFO_THRESHOLD(bytes)		((bytes) / 16)
#define RDMA_FIFO_SIZE(rdma)			((rdma)->data->fifo_size)
#define DISP_RDMA_MEM_START_ADDR		0x0f00

#define RDMA_MEM_GMC				0x40402020

#define RDMA_SOFT_RESET				BIT(4)
#define RDMA_RESET_STATE				GENMASK(10, 8)
#define RDMA_RESET_IDLE				BIT(8)
#define DISP_RDMA_MEM_GMC_SETTING_1		0x0034
#define DISP_RDMA_MEM_GMC_SETTING_2		0x003c
#define RDMA_GMC_VDE				BIT(31)
#define DISP_RDMA_BG_CON_0			0x00a0
#define DISP_RDMA_BG_CON_1			0x00a4
#define DISP_RDMA_THRESHOLD_FOR_SODI		0x00a8
#define DISP_RDMA_THRESHOLD_FOR_DVFS		0x00ac
#define DISP_RDMA_SRAM_SEL			0x00b0
#define DISP_RDMA_DRAM_CON			0x00c0
#define DISP_RDMA_DVFS_SETTING_PRE		0x00d0
#define DISP_RDMA_DVFS_SETTING_ULTRA		0x00d4
#define DISP_RDMA_LEAVE_DRS_SETTING		0x00d8
#define DISP_RDMA_ENTER_DRS_SETTING		0x00dc
#define DISP_RDMA_MEM_GMC_SETTING_3		0x00e8
#define MT6765_RDMA_FIFO_WORDS			384

static const u32 mt8173_formats[] = {
	DRM_FORMAT_XRGB8888,
	DRM_FORMAT_ARGB8888,
	DRM_FORMAT_BGRX8888,
	DRM_FORMAT_BGRA8888,
	DRM_FORMAT_ABGR8888,
	DRM_FORMAT_XBGR8888,
	DRM_FORMAT_RGB888,
	DRM_FORMAT_BGR888,
	DRM_FORMAT_RGB565,
	DRM_FORMAT_UYVY,
	DRM_FORMAT_YUYV,
};

struct mtk_disp_rdma_data {
	unsigned int fifo_size;
	const u32 *formats;
	size_t num_formats;
	bool mt6765;
};

/*
 * struct mtk_disp_rdma - DISP_RDMA driver structure
 * @data: local driver data
 */
struct mtk_disp_rdma {
	struct clk			*clk;
	void __iomem			*regs;
	struct cmdq_client_reg		cmdq_reg;
	const struct mtk_disp_rdma_data	*data;
	void				(*vblank_cb)(void *data);
	void				*vblank_cb_data;
	u32				fifo_size;
	unsigned long			clock_rate;
	unsigned int			width, height, vrefresh, bpc;
	bool				config_valid;
};

struct mt6765_rdma_fifo {
	u32 pre_ultra, ultra, request, sodi, dvfs, dvfs_pre, dvfs_ultra, urgent;
};

static irqreturn_t mtk_disp_rdma_irq_handler(int irq, void *dev_id)
{
	struct mtk_disp_rdma *priv = dev_id;

	/* Clear frame completion interrupt */
	writel(0x0, priv->regs + DISP_REG_RDMA_INT_STATUS);

	if (!priv->vblank_cb)
		return IRQ_NONE;

	priv->vblank_cb(priv->vblank_cb_data);

	return IRQ_HANDLED;
}

static void rdma_update_bits(struct device *dev, unsigned int reg,
			     unsigned int mask, unsigned int val)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);
	unsigned int tmp = readl(rdma->regs + reg);

	tmp = (tmp & ~mask) | (val & mask);
	writel(tmp, rdma->regs + reg);
}

void mtk_rdma_register_vblank_cb(struct device *dev,
				 void (*vblank_cb)(void *),
				 void *vblank_cb_data)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	rdma->vblank_cb = vblank_cb;
	rdma->vblank_cb_data = vblank_cb_data;
}

void mtk_rdma_unregister_vblank_cb(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	rdma->vblank_cb = NULL;
	rdma->vblank_cb_data = NULL;
}

void mtk_rdma_enable_vblank(struct device *dev)
{
	rdma_update_bits(dev, DISP_REG_RDMA_INT_ENABLE, RDMA_FRAME_END_INT,
			 RDMA_FRAME_END_INT);
}

void mtk_rdma_disable_vblank(struct device *dev)
{
	rdma_update_bits(dev, DISP_REG_RDMA_INT_ENABLE, RDMA_FRAME_END_INT, 0);
}

const u32 *mtk_rdma_get_formats(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	return rdma->data->formats;
}

size_t mtk_rdma_get_num_formats(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	return rdma->data->num_formats;
}

int mtk_rdma_clk_enable(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);
	u32 value;
	int ret;

	ret = clk_prepare_enable(rdma->clk);
	if (ret || !rdma->data->mt6765)
		return ret;

	/* Configuration can run in IRQ context; sample the rate while sleepable. */
	rdma->clock_rate = clk_get_rate(rdma->clk);
	rdma->config_valid = false;
	if (!rdma->clock_rate || rdma->clock_rate > U32_MAX) {
		ret = -EINVAL;
		goto disable_clock;
	}

	/* Quiesce LK's engine before changing its input and FIFO ownership. */
	rdma_update_bits(dev, DISP_REG_RDMA_GLOBAL_CON, RDMA_ENGINE_EN, 0);
	writel(0, rdma->regs + DISP_REG_RDMA_INT_ENABLE);
	writel(0, rdma->regs + DISP_REG_RDMA_INT_STATUS);
	rdma_update_bits(dev, DISP_REG_RDMA_GLOBAL_CON, RDMA_SOFT_RESET, RDMA_SOFT_RESET);
	ret = readl_poll_timeout(rdma->regs + DISP_REG_RDMA_GLOBAL_CON, value,
				(value & RDMA_RESET_STATE) != RDMA_RESET_IDLE, 10, 100000);
	/* Always release reset, including a timeout while entering reset. */
	rdma_update_bits(dev, DISP_REG_RDMA_GLOBAL_CON, RDMA_SOFT_RESET, 0);
	if (ret)
		goto disable_clock;
	ret = readl_poll_timeout(rdma->regs + DISP_REG_RDMA_GLOBAL_CON, value,
				(value & RDMA_RESET_STATE) == RDMA_RESET_IDLE, 10, 100000);
	if (!ret)
		return 0;

disable_clock:
	dev_err(dev, "RDMA clock/reset failed: %d\n", ret);
	rdma->clock_rate = 0;
	clk_disable_unprepare(rdma->clk);
	return ret;
}

void mtk_rdma_clk_disable(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	clk_disable_unprepare(rdma->clk);
	rdma->config_valid = false;
	rdma->clock_rate = 0;
}

void mtk_rdma_start(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	if (rdma->data->mt6765 && !rdma->config_valid)
		return;

	rdma_update_bits(dev, DISP_REG_RDMA_GLOBAL_CON, RDMA_ENGINE_EN,
			 RDMA_ENGINE_EN);
}

void mtk_rdma_stop(struct device *dev)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);

	rdma_update_bits(dev, DISP_REG_RDMA_GLOBAL_CON, RDMA_ENGINE_EN, 0);
	if (rdma->data->mt6765) {
		writel(0, rdma->regs + DISP_REG_RDMA_INT_ENABLE);
		writel(0, rdma->regs + DISP_REG_RDMA_INT_STATUS);
		rdma->config_valid = false;
	}
}

static int mt6765_rdma_calc_fifo(struct mtk_disp_rdma *rdma,
				unsigned int width, unsigned int height,
				unsigned int vrefresh, unsigned int bpc, bool memory,
				struct mt6765_rdma_fifo *fifo)
{
	u64 bandwidth, consume, fill;
	u32 pre_low, pre_high, ultra_low, sodi_high;
	s64 remaining;

	if (!width || width > GENMASK(12, 0) || !height || height > GENMASK(19, 0) ||
	    !vrefresh || bpc < 6 || bpc > 10 || !rdma->clock_rate)
		return -EINVAL;
	if (check_mul_overflow((u64)width * height, (u64)vrefresh * (bpc * 3 / 8),
			       &bandwidth))
		return -ERANGE;

	/* Stock rounding, in 16-byte words/us, including its 25% margin. */
	consume = DIV_ROUND_UP_ULL(div_u64(div_u64(bandwidth, 1000) * 5, 6400), 10);
	fill = DIV_ROUND_UP_ULL((u64)rdma->clock_rate * (memory ? 960 : 180), 1000000000);
	/* Bound request/DVFS thresholds before subtracting the FIFO depth. */
	if (consume >= fill || consume * 9 >= MT6765_RDMA_FIFO_WORDS)
		return -ERANGE;

	pre_low = consume * 6;
	pre_high = consume * 7;
	ultra_low = consume * 4;
	remaining = MT6765_RDMA_FIFO_WORDS - (s64)(fill - consume) * 5;
	sodi_high = max_t(s64, pre_high, remaining);
	fifo->pre_ultra = RDMA_GMC_VDE | pre_low | (pre_high << 16);
	fifo->ultra = RDMA_GMC_VDE | ultra_low | (pre_low << 16);
	fifo->request = min_t(u32, MT6765_RDMA_FIFO_WORDS - pre_low, 255);
	fifo->sodi = consume * 9 | (sodi_high << 16);
	fifo->dvfs = pre_low | (pre_low << 16);
	fifo->dvfs_pre = consume * 8 | ((consume * 9) << 16);
	fifo->dvfs_ultra = consume * 6 | ((consume * 8) << 16);
	fifo->urgent = consume * 3 | (DIV_ROUND_UP_ULL(consume * 35, 10) << 16);
	return 0;
}

static void mt6765_rdma_write_fifo(struct mtk_disp_rdma *rdma,
				  const struct mt6765_rdma_fifo *fifo,
				  struct cmdq_pkt *cmdq_pkt)
{
	mtk_ddp_write(cmdq_pkt, fifo->pre_ultra, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_MEM_GMC_SETTING_0);
	mtk_ddp_write(cmdq_pkt, fifo->ultra, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_MEM_GMC_SETTING_1);
	mtk_ddp_write(cmdq_pkt, fifo->request, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_MEM_GMC_SETTING_2);
	/* Video mode drains immediately; no RSZ/WROT SRAM is borrowed. */
	mtk_ddp_write(cmdq_pkt, RDMA_FIFO_UNDERFLOW_EN | (MT6765_RDMA_FIFO_WORDS << 16),
		      &rdma->cmdq_reg, rdma->regs, DISP_REG_RDMA_FIFO_CON);
	mtk_ddp_write(cmdq_pkt, fifo->sodi, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_THRESHOLD_FOR_SODI);
	mtk_ddp_write(cmdq_pkt, fifo->dvfs, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_THRESHOLD_FOR_DVFS);
	mtk_ddp_write(cmdq_pkt, BIT(16), &rdma->cmdq_reg, rdma->regs, DISP_RDMA_DRAM_CON);
	mtk_ddp_write(cmdq_pkt, fifo->dvfs_pre, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_DVFS_SETTING_PRE);
	mtk_ddp_write(cmdq_pkt, fifo->dvfs_ultra, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_DVFS_SETTING_ULTRA);
	mtk_ddp_write(cmdq_pkt, fifo->urgent, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_LEAVE_DRS_SETTING);
	mtk_ddp_write(cmdq_pkt, fifo->urgent, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_ENTER_DRS_SETTING);
	mtk_ddp_write(cmdq_pkt, fifo->urgent, &rdma->cmdq_reg, rdma->regs,
		      DISP_RDMA_MEM_GMC_SETTING_3);
	mtk_ddp_write(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs, DISP_RDMA_SRAM_SEL);
}

static void mt6765_rdma_config(struct device *dev, unsigned int width,
			       unsigned int height, unsigned int vrefresh,
			       unsigned int bpc, struct cmdq_pkt *cmdq_pkt)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);
	struct mt6765_rdma_fifo fifo;
	int ret;

	/* The CRTC's later pending-mode callback passes bpc = 0. */
	if (!bpc)
		bpc = rdma->bpc ? rdma->bpc : 8;
	rdma->config_valid = false;
	ret = mt6765_rdma_calc_fifo(rdma, width, height, vrefresh, bpc, false, &fifo);
	if (ret) {
		dev_err_ratelimited(dev, "RDMA mode %ux%u@%u, %u bpc, %lu Hz: %d\n",
				    width, height, vrefresh, bpc, rdma->clock_rate, ret);
		if (rdma->clock_rate)
			mtk_ddp_write_mask(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs,
					   DISP_REG_RDMA_GLOBAL_CON, RDMA_ENGINE_EN);
		return;
	}

	/* Establish direct-link RGB input instead of inheriting LK's memory mode. */
	mtk_ddp_write_mask(cmdq_pkt, width, &rdma->cmdq_reg, rdma->regs,
			   DISP_REG_RDMA_SIZE_CON_0,
			   GENMASK(12, 0) | RDMA_MATRIX_ENABLE | RDMA_MATRIX_INT_MTX_SEL);
	mtk_ddp_write_mask(cmdq_pkt, height, &rdma->cmdq_reg, rdma->regs,
			   DISP_REG_RDMA_SIZE_CON_1, GENMASK(19, 0));
	mtk_ddp_write_mask(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs,
			   DISP_REG_RDMA_GLOBAL_CON, RDMA_MODE_MEMORY);
	mtk_ddp_write_mask(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs,
			   DISP_RDMA_MEM_CON, GENMASK(8, 4));
	mtk_ddp_write(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs, DISP_RDMA_MEM_START_ADDR);
	mtk_ddp_write(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs, DISP_RDMA_MEM_SRC_PITCH);
	mtk_ddp_write(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs, DISP_RDMA_BG_CON_0);
	mtk_ddp_write(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs, DISP_RDMA_BG_CON_1);
	mt6765_rdma_write_fifo(rdma, &fifo, cmdq_pkt);
	rdma->width = width;
	rdma->height = height;
	rdma->vrefresh = vrefresh;
	rdma->bpc = bpc;
	rdma->config_valid = true;
}

void mtk_rdma_config(struct device *dev, unsigned int width,
		     unsigned int height, unsigned int vrefresh,
		     unsigned int bpc, struct cmdq_pkt *cmdq_pkt)
{
	unsigned int threshold;
	unsigned int reg;
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);
	u32 rdma_fifo_size;

	if (rdma->data->mt6765) {
		mt6765_rdma_config(dev, width, height, vrefresh, bpc, cmdq_pkt);
		return;
	}

	mtk_ddp_write_mask(cmdq_pkt, width, &rdma->cmdq_reg, rdma->regs,
			   DISP_REG_RDMA_SIZE_CON_0, 0xfff);
	mtk_ddp_write_mask(cmdq_pkt, height, &rdma->cmdq_reg, rdma->regs,
			   DISP_REG_RDMA_SIZE_CON_1, 0xfffff);

	if (rdma->fifo_size)
		rdma_fifo_size = rdma->fifo_size;
	else
		rdma_fifo_size = RDMA_FIFO_SIZE(rdma);

	/*
	 * Enable FIFO underflow since DSI and DPI can't be blocked.
	 * Keep the FIFO pseudo size reset default of 8 KiB. Set the
	 * output threshold to 70% of max fifo size to make sure the
	 * threhold will not overflow
	 */
	threshold = rdma_fifo_size * 7 / 10;
	reg = RDMA_FIFO_UNDERFLOW_EN |
	      RDMA_FIFO_PSEUDO_SIZE(rdma_fifo_size) |
	      RDMA_OUTPUT_VALID_FIFO_THRESHOLD(threshold);
	mtk_ddp_write(cmdq_pkt, reg, &rdma->cmdq_reg, rdma->regs, DISP_REG_RDMA_FIFO_CON);
}

static unsigned int rdma_fmt_convert(struct mtk_disp_rdma *rdma,
				     unsigned int fmt)
{
	/* The return value in switch "MEM_MODE_INPUT_FORMAT_XXX"
	 * is defined in mediatek HW data sheet.
	 * The alphabet order in XXX is no relation to data
	 * arrangement in memory.
	 */
	switch (fmt) {
	default:
	case DRM_FORMAT_RGB565:
		return MEM_MODE_INPUT_FORMAT_RGB565;
	case DRM_FORMAT_BGR565:
		return MEM_MODE_INPUT_FORMAT_RGB565 | MEM_MODE_INPUT_SWAP;
	case DRM_FORMAT_RGB888:
		return MEM_MODE_INPUT_FORMAT_RGB888;
	case DRM_FORMAT_BGR888:
		return MEM_MODE_INPUT_FORMAT_RGB888 | MEM_MODE_INPUT_SWAP;
	case DRM_FORMAT_RGBX8888:
	case DRM_FORMAT_RGBA8888:
		return MEM_MODE_INPUT_FORMAT_ARGB8888;
	case DRM_FORMAT_BGRX8888:
	case DRM_FORMAT_BGRA8888:
		return MEM_MODE_INPUT_FORMAT_ARGB8888 | MEM_MODE_INPUT_SWAP;
	case DRM_FORMAT_XRGB8888:
	case DRM_FORMAT_ARGB8888:
		return MEM_MODE_INPUT_FORMAT_RGBA8888;
	case DRM_FORMAT_XBGR8888:
	case DRM_FORMAT_ABGR8888:
		return MEM_MODE_INPUT_FORMAT_RGBA8888 | MEM_MODE_INPUT_SWAP;
	case DRM_FORMAT_UYVY:
		return MEM_MODE_INPUT_FORMAT_UYVY;
	case DRM_FORMAT_YUYV:
		return MEM_MODE_INPUT_FORMAT_YUYV;
	}
}

unsigned int mtk_rdma_layer_nr(struct device *dev)
{
	return 1;
}

void mtk_rdma_layer_config(struct device *dev, unsigned int idx,
			   struct mtk_plane_state *state,
			   struct cmdq_pkt *cmdq_pkt)
{
	struct mtk_disp_rdma *rdma = dev_get_drvdata(dev);
	struct mtk_plane_pending_state *pending = &state->pending;
	unsigned int addr = pending->addr;
	unsigned int pitch = pending->pitch & 0xffff;
	unsigned int fmt = pending->format;
	unsigned int con;
	struct mt6765_rdma_fifo fifo;

	if (rdma->data->mt6765 &&
	    (!rdma->config_valid ||
	     mt6765_rdma_calc_fifo(rdma, rdma->width, rdma->height, rdma->vrefresh,
				  rdma->bpc, true, &fifo))) {
		dev_err_ratelimited(dev, "RDMA memory input is not configured\n");
		return;
	}

	con = rdma_fmt_convert(rdma, fmt);
	mtk_ddp_write_relaxed(cmdq_pkt, con, &rdma->cmdq_reg, rdma->regs, DISP_RDMA_MEM_CON);

	if (fmt == DRM_FORMAT_UYVY || fmt == DRM_FORMAT_YUYV) {
		mtk_ddp_write_mask(cmdq_pkt, RDMA_MATRIX_ENABLE, &rdma->cmdq_reg, rdma->regs,
				   DISP_REG_RDMA_SIZE_CON_0,
				   RDMA_MATRIX_ENABLE);
		mtk_ddp_write_mask(cmdq_pkt, RDMA_MATRIX_INT_MTX_BT601_to_RGB,
				   &rdma->cmdq_reg, rdma->regs, DISP_REG_RDMA_SIZE_CON_0,
				   RDMA_MATRIX_INT_MTX_SEL);
	} else {
		mtk_ddp_write_mask(cmdq_pkt, 0, &rdma->cmdq_reg, rdma->regs,
				   DISP_REG_RDMA_SIZE_CON_0,
				   RDMA_MATRIX_ENABLE);
	}
	mtk_ddp_write_relaxed(cmdq_pkt, addr, &rdma->cmdq_reg, rdma->regs,
			      DISP_RDMA_MEM_START_ADDR);
	mtk_ddp_write_relaxed(cmdq_pkt, pitch, &rdma->cmdq_reg, rdma->regs,
			      DISP_RDMA_MEM_SRC_PITCH);
	if (rdma->data->mt6765)
		mt6765_rdma_write_fifo(rdma, &fifo, cmdq_pkt);
	else
		mtk_ddp_write(cmdq_pkt, RDMA_MEM_GMC, &rdma->cmdq_reg, rdma->regs,
			      DISP_RDMA_MEM_GMC_SETTING_0);
	mtk_ddp_write_mask(cmdq_pkt, RDMA_MODE_MEMORY, &rdma->cmdq_reg, rdma->regs,
			   DISP_REG_RDMA_GLOBAL_CON, RDMA_MODE_MEMORY);

}

static int mtk_disp_rdma_bind(struct device *dev, struct device *master,
			      void *data)
{
	return 0;

}

static void mtk_disp_rdma_unbind(struct device *dev, struct device *master,
				 void *data)
{
}

static const struct component_ops mtk_disp_rdma_component_ops = {
	.bind	= mtk_disp_rdma_bind,
	.unbind = mtk_disp_rdma_unbind,
};

static int mtk_disp_rdma_probe(struct platform_device *pdev)
{
	struct device *dev = &pdev->dev;
	struct mtk_disp_rdma *priv;
	int irq;
	int ret;

	priv = devm_kzalloc(dev, sizeof(*priv), GFP_KERNEL);
	if (!priv)
		return -ENOMEM;

	irq = platform_get_irq(pdev, 0);
	if (irq < 0)
		return irq;

	priv->clk = devm_clk_get(dev, NULL);
	if (IS_ERR(priv->clk))
		return dev_err_probe(dev, PTR_ERR(priv->clk),
				     "failed to get rdma clk\n");

	priv->regs = devm_platform_ioremap_resource(pdev, 0);
	if (IS_ERR(priv->regs))
		return dev_err_probe(dev, PTR_ERR(priv->regs),
				     "failed to ioremap rdma\n");
#if IS_REACHABLE(CONFIG_MTK_CMDQ)
	ret = cmdq_dev_get_client_reg(dev, &priv->cmdq_reg, 0);
	if (ret)
		dev_dbg(dev, "get mediatek,gce-client-reg fail!\n");
#endif

	ret = of_property_read_u32(dev->of_node,
				   "mediatek,rdma-fifo-size",
				   &priv->fifo_size);
	if (ret && (ret != -EINVAL))
		return dev_err_probe(dev, ret, "Failed to get rdma fifo size\n");

	priv->data = of_device_get_match_data(dev);
	if (priv->data->mt6765 && priv->fifo_size)
		return dev_err_probe(dev, -EINVAL, "MT6765 uses its unshared RDMA0 FIFO\n");

	/* Disable and clear pending interrupts */
	writel(0x0, priv->regs + DISP_REG_RDMA_INT_ENABLE);
	writel(0x0, priv->regs + DISP_REG_RDMA_INT_STATUS);

	ret = devm_request_irq(dev, irq, mtk_disp_rdma_irq_handler,
			       IRQF_TRIGGER_NONE, dev_name(dev), priv);
	if (ret < 0)
		return dev_err_probe(dev, ret, "Failed to request irq %d\n", irq);

	platform_set_drvdata(pdev, priv);

	pm_runtime_enable(dev);

	ret = component_add(dev, &mtk_disp_rdma_component_ops);
	if (ret) {
		pm_runtime_disable(dev);
		return dev_err_probe(dev, ret, "Failed to add component\n");
	}

	return 0;
}

static void mtk_disp_rdma_remove(struct platform_device *pdev)
{
	component_del(&pdev->dev, &mtk_disp_rdma_component_ops);

	pm_runtime_disable(&pdev->dev);
}

static const struct mtk_disp_rdma_data mt2701_rdma_driver_data = {
	.fifo_size = SZ_4K,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_rdma_data mt8173_rdma_driver_data = {
	.fifo_size = SZ_8K,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_rdma_data mt6765_rdma_driver_data = {
	.fifo_size = 6 * SZ_1K,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
	.mt6765 = true,
};

static const struct mtk_disp_rdma_data mt8183_rdma_driver_data = {
	.fifo_size = 5 * SZ_1K,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct mtk_disp_rdma_data mt8195_rdma_driver_data = {
	.fifo_size = 1920,
	.formats = mt8173_formats,
	.num_formats = ARRAY_SIZE(mt8173_formats),
};

static const struct of_device_id mtk_disp_rdma_driver_dt_match[] = {
	{ .compatible = "mediatek,mt6765-disp-rdma",
	  .data = &mt6765_rdma_driver_data },
	{ .compatible = "mediatek,mt2701-disp-rdma",
	  .data = &mt2701_rdma_driver_data},
	{ .compatible = "mediatek,mt8173-disp-rdma",
	  .data = &mt8173_rdma_driver_data},
	{ .compatible = "mediatek,mt8183-disp-rdma",
	  .data = &mt8183_rdma_driver_data},
	{ .compatible = "mediatek,mt8195-disp-rdma",
	  .data = &mt8195_rdma_driver_data},
	{},
};
MODULE_DEVICE_TABLE(of, mtk_disp_rdma_driver_dt_match);

struct platform_driver mtk_disp_rdma_driver = {
	.probe		= mtk_disp_rdma_probe,
	.remove		= mtk_disp_rdma_remove,
	.driver		= {
		.name	= "mediatek-disp-rdma",
		.of_match_table = mtk_disp_rdma_driver_dt_match,
	},
};
