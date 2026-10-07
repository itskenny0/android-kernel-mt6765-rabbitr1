// SPDX-License-Identifier: GPL-2.0-only
/*
 * rabbit r1 480x640 panel, using the RabbitOS v0.8.293 command sequence.
 * The vendor name ili9883_boe_mipi_hd does not identify the fitted controller:
 * the active sequence uses ST7701-style command banks and compare_id accepts
 * every result. Keep the board-specific name until the part is identified.
 *
 * This bring-up driver relies on the power rails established by firmware.
 * Panel supply routing and cold power-on sequencing still need board evidence.
 */

#include <linux/delay.h>
#include <linux/gpio/consumer.h>
#include <linux/mod_devicetable.h>
#include <linux/module.h>

#include <drm/drm_mipi_dsi.h>
#include <drm/drm_modes.h>
#include <drm/drm_panel.h>

struct r1_panel {
	struct drm_panel panel;
	struct mipi_dsi_device *dsi;
	struct gpio_desc *reset;
};

struct r1_panel_command {
	u16 delay_ms;
	u8 len;
	u8 data[17];
};

/* Preserve payload lengths, including the extra zero on 0x11 and 0x29. */
static const struct r1_panel_command r1_panel_init[] = {
	{ .delay_ms = 120 },
	{ .len = 6, .data = { 0xff, 0x77, 0x01, 0x00, 0x00, 0x13 } },
	{ .len = 2, .data = { 0xef, 0x08 } },
	{ .len = 6, .data = { 0xff, 0x77, 0x01, 0x00, 0x00, 0x10 } },
	{ .len = 3, .data = { 0xc0, 0x4f, 0x00 } },
	{ .len = 3, .data = { 0xc1, 0x10, 0x0c } },
	{ .len = 3, .data = { 0xc2, 0x07, 0x14 } },
	{ .len = 2, .data = { 0xcc, 0x10 } },
	{ .len = 17, .data = { 0xb0, 0x0a, 0x18, 0x1e, 0x12, 0x16, 0x0c, 0x0e,
		   0x0d, 0x0c, 0x29, 0x06, 0x14, 0x13, 0x29, 0x33,
		   0x1c } },
	{ .len = 17, .data = { 0xb1, 0x0a, 0x19, 0x21, 0x0a, 0x0c, 0x00, 0x0c,
		   0x03, 0x03, 0x23, 0x01, 0x0e, 0x0c, 0x27, 0x2b,
		   0x1c } },
	{ .len = 2, .data = { 0xe5, 0x00 } },
	{ .len = 6, .data = { 0xff, 0x77, 0x01, 0x00, 0x00, 0x11 } },
	{ .len = 2, .data = { 0xb0, 0x5d } },
	{ .len = 2, .data = { 0xb1, 0x63 } },
	{ .len = 2, .data = { 0xb2, 0x84 } },
	{ .len = 2, .data = { 0xb3, 0x80 } },
	{ .len = 2, .data = { 0xb5, 0x4d } },
	{ .len = 2, .data = { 0xb7, 0x85 } },
	{ .len = 2, .data = { 0xb8, 0x20 } },
	{ .len = 2, .data = { 0xc1, 0x78 } },
	{ .len = 2, .data = { 0xc2, 0x78 } },
	{ .len = 2, .data = { 0xd0, 0x88 } },
	{ .len = 4, .data = { 0xe0, 0x00, 0x00, 0x02 } },
	{ .len = 12, .data = { 0xe1, 0x06, 0xa0, 0x08, 0xa0, 0x05, 0xa0, 0x07,
		   0xa0, 0x00, 0x44, 0x44 } },
	{ .len = 13, .data = { 0xe2, 0x20, 0x20, 0x44, 0x44, 0x96, 0xa0, 0x00,
		   0x00, 0x96, 0xa0, 0x00, 0x00 } },
	{ .len = 5, .data = { 0xe3, 0x00, 0x00, 0x22, 0x22 } },
	{ .len = 3, .data = { 0xe4, 0x44, 0x44 } },
	{ .len = 17, .data = { 0xe5, 0x0d, 0x91, 0x0a, 0xa0, 0x0f, 0x93, 0x0a,
		   0xa0, 0x09, 0x8d, 0x0a, 0xa0, 0x0b, 0x8f, 0x0a,
		   0xa0 } },
	{ .len = 5, .data = { 0xe6, 0x00, 0x00, 0x22, 0x22 } },
	{ .len = 3, .data = { 0xe7, 0x44, 0x44 } },
	{ .len = 17, .data = { 0xe8, 0x0c, 0x90, 0x0a, 0xa0, 0x0e, 0x92, 0x0a,
		   0xa0, 0x08, 0x8c, 0x0a, 0xa0, 0x0a, 0x8e, 0x0a,
		   0xa0 } },
	{ .len = 3, .data = { 0xe9, 0x36, 0x00 } },
	{ .len = 8, .data = { 0xeb, 0x00, 0x01, 0xe4, 0xe4, 0x44, 0x88, 0x40 } },
	{ .len = 17, .data = { 0xed, 0xff, 0x45, 0x67, 0xfa, 0x01, 0x2b, 0xcf,
		   0xff, 0xff, 0xfc, 0xb2, 0x10, 0xaf, 0x76, 0x54,
		   0xff } },
	{ .len = 7, .data = { 0xef, 0x10, 0x0d, 0x04, 0x08, 0x3f, 0x1f } },
	{ .len = 6, .data = { 0xff, 0x77, 0x01, 0x00, 0x00, 0x10 } },
	{ .len = 2, .data = { 0x11, 0x00 } },
	{ .delay_ms = 120 },
	{ .len = 2, .data = { 0x35, 0x00 } },
	{ .len = 2, .data = { 0x36, 0x00 } },
	{ .len = 2, .data = { 0x29, 0x00 } },
	{ .delay_ms = 60 },
};

static inline struct r1_panel *to_r1_panel(struct drm_panel *panel)
{
	return container_of(panel, struct r1_panel, panel);
}

static int r1_panel_write(struct r1_panel *ctx, const u8 *data, size_t len)
{
	ssize_t ret;

	/* Stock DSI_set_cmdq_V2 sends commands >= 0xb0 as generic packets. */
	if (data[0] < 0xb0)
		ret = mipi_dsi_dcs_write_buffer(ctx->dsi, data, len);
	else
		ret = mipi_dsi_generic_write(ctx->dsi, data, len);
	if (ret < 0)
		return ret;

	return ret == len ? 0 : -EIO;
}

static int r1_panel_prepare(struct drm_panel *panel)
{
	struct r1_panel *ctx = to_r1_panel(panel);
	unsigned int i;
	int ret;

	/* Active-low descriptor: physical high 50ms, low 50ms, high 150ms. */
	ret = gpiod_direction_output(ctx->reset, 0);
	if (ret)
		return ret;
	msleep(50);
	gpiod_set_value_cansleep(ctx->reset, 1);
	msleep(50);
	gpiod_set_value_cansleep(ctx->reset, 0);
	msleep(150);

	for (i = 0; i < ARRAY_SIZE(r1_panel_init); i++) {
		const struct r1_panel_command *cmd = &r1_panel_init[i];

		if (cmd->delay_ms) {
			msleep(cmd->delay_ms);
			continue;
		}
		ret = r1_panel_write(ctx, cmd->data, cmd->len);
		if (ret) {
			dev_err(&ctx->dsi->dev, "Panel command %u failed: %d\n", i, ret);
			gpiod_set_value_cansleep(ctx->reset, 1);
			msleep(120);
			return ret;
		}
	}

	return 0;
}

static int r1_panel_unprepare(struct drm_panel *panel)
{
	struct r1_panel *ctx = to_r1_panel(panel);
	static const u8 display_off[] = { 0x28 };
	static const u8 sleep_in[] = { 0x10 };
	int ret, sleep_ret;

	ret = r1_panel_write(ctx, display_off, sizeof(display_off));
	msleep(50);
	/* Still attempt sleep and reset when display-off fails. */
	sleep_ret = r1_panel_write(ctx, sleep_in, sizeof(sleep_in));
	msleep(250);
	msleep(50);
	gpiod_set_value_cansleep(ctx->reset, 1);
	msleep(120);

	if (ret || sleep_ret)
		dev_warn(&ctx->dsi->dev, "Panel sleep commands failed: %d, %d\n", ret, sleep_ret);
	/* Reset completed: let the panel core clear its prepared state. */
	return 0;
}

static int r1_panel_enable(struct drm_panel *panel)
{
	/* The panel core can call enable after a failed prepare. */
	return panel->prepared ? 0 : -EPERM;
}

static const struct drm_display_mode r1_panel_mode = {
	/* 130 MHz vendor PLL * 2 * 2 lanes / 24 bits, rounded to kHz.
	 * This preserves the link-rate request; refresh needs measurement.
	 */
	.clock = 21667,
	.hdisplay = 480,
	.hsync_start = 480 + 20,
	.hsync_end = 480 + 20 + 20,
	.htotal = 480 + 20 + 20 + 20,
	.vdisplay = 640,
	.vsync_start = 640 + 26,
	.vsync_end = 640 + 26 + 2,
	.vtotal = 640 + 26 + 2 + 14,
};

static int r1_panel_get_modes(struct drm_panel *panel,
			      struct drm_connector *connector)
{
	struct drm_display_mode *mode;

	mode = drm_mode_duplicate(connector->dev, &r1_panel_mode);
	if (!mode)
		return -ENOMEM;

	mode->type = DRM_MODE_TYPE_DRIVER | DRM_MODE_TYPE_PREFERRED;
	drm_mode_set_name(mode);
	drm_mode_probed_add(connector, mode);
	connector->display_info.bpc = 8;

	return 1;
}

static const struct drm_panel_funcs r1_panel_funcs = {
	.prepare = r1_panel_prepare,
	.enable = r1_panel_enable,
	.unprepare = r1_panel_unprepare,
	.get_modes = r1_panel_get_modes,
};

static int r1_panel_probe(struct mipi_dsi_device *dsi)
{
	struct device *dev = &dsi->dev;
	struct r1_panel *ctx;
	int ret;

	ctx = devm_drm_panel_alloc(dev, struct r1_panel, panel,
				   &r1_panel_funcs, DRM_MODE_CONNECTOR_DSI);
	if (IS_ERR(ctx))
		return PTR_ERR(ctx);

	/* Leave the firmware's reset level alone until DRM prepares the panel. */
	ctx->reset = devm_gpiod_get(dev, "reset", GPIOD_ASIS);
	if (IS_ERR(ctx->reset))
		return dev_err_probe(dev, PTR_ERR(ctx->reset), "Cannot get panel reset\n");

	ctx->dsi = dsi;
	ctx->panel.prepare_prev_first = true;
	dsi->lanes = 2;
	dsi->format = MIPI_DSI_FMT_RGB888;
	dsi->mode_flags = MIPI_DSI_MODE_VIDEO | MIPI_DSI_MODE_VIDEO_SYNC_PULSE |
			  MIPI_DSI_MODE_LPM;
	mipi_dsi_set_drvdata(dsi, ctx);

	ret = drm_panel_of_backlight(&ctx->panel);
	if (ret)
		return ret;

	drm_panel_add(&ctx->panel);
	ret = mipi_dsi_attach(dsi);
	if (ret) {
		drm_panel_remove(&ctx->panel);
		return dev_err_probe(dev, ret, "Cannot attach panel to DSI\n");
	}

	return 0;
}

static void r1_panel_remove(struct mipi_dsi_device *dsi)
{
	struct r1_panel *ctx = mipi_dsi_get_drvdata(dsi);

	mipi_dsi_detach(dsi);
	drm_panel_remove(&ctx->panel);
}

static const struct of_device_id r1_panel_of_match[] = {
	{ .compatible = "rabbit,r1-panel" },
	{ }
};
MODULE_DEVICE_TABLE(of, r1_panel_of_match);

static struct mipi_dsi_driver r1_panel_driver = {
	.probe = r1_panel_probe,
	.remove = r1_panel_remove,
	.driver = {
		.name = "panel-rabbit-r1",
		.of_match_table = r1_panel_of_match,
	},
};
module_mipi_dsi_driver(r1_panel_driver);

MODULE_DESCRIPTION("rabbit r1 firmware-powered MIPI DSI panel");
MODULE_LICENSE("GPL");
