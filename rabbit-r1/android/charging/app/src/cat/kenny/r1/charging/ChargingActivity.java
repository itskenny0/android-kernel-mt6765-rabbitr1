// SPDX-License-Identifier: Apache-2.0
package cat.kenny.r1.charging;

import android.app.Activity;
import android.app.AlertDialog;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.UserManager;
import android.view.MenuItem;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.io.FileInputStream;
import java.nio.charset.StandardCharsets;
import java.util.HashMap;
import java.util.Map;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

public final class ChargingActivity extends Activity {
    static { System.loadLibrary("r1_charging_jni"); }
    private static native String getChoice();
    private static native boolean setChoice(String choice);
    private static final String[] VALUES = {"auto", "500", "600", "700", "800", "900", "1000"};
    private static final String STATUS = "/sys/devices/platform/charging-policy/status";
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final ExecutorService reader = Executors.newSingleThreadExecutor();
    private Button choiceButton;
    private TextView statusView;
    private String[] labels;
    private boolean resumed;
    private boolean reading;
    private final Runnable refresh = this::refresh;

    @Override public void onCreate(Bundle saved) {
        super.onCreate(saved);
        if (!getSystemService(UserManager.class).isSystemUser()) {
            finish();
            return;
        }
        if (getActionBar() != null) getActionBar().setDisplayHomeAsUpEnabled(true);
        labels = new String[VALUES.length];
        labels[0] = getString(R.string.automatic);
        for (int i = 1; i < VALUES.length; i++) {
            labels[i] = getString(R.string.milliamps, Integer.parseInt(VALUES[i]));
        }
        ScrollView scroll = new ScrollView(this);
        LinearLayout layout = new LinearLayout(this);
        layout.setOrientation(LinearLayout.VERTICAL);
        int padding = Math.round(16 * getResources().getDisplayMetrics().density);
        layout.setPadding(padding, padding, padding, padding);
        choiceButton = new Button(this);
        choiceButton.setOnClickListener(v -> new AlertDialog.Builder(this)
                .setTitle(R.string.app_name)
                .setSingleChoiceItems(labels, indexOf(getChoice()), (dialog, which) -> {
                    if (!setChoice(VALUES[which])) {
                        Toast.makeText(this, R.string.save_failed, Toast.LENGTH_LONG).show();
                    }
                    dialog.dismiss();
                    refresh();
                }).setNegativeButton(android.R.string.cancel, null).show());
        statusView = new TextView(this);
        statusView.setAccessibilityLiveRegion(TextView.ACCESSIBILITY_LIVE_REGION_POLITE);
        TextView explanation = new TextView(this);
        explanation.setText(R.string.explanation);
        explanation.setPadding(0, padding, 0, 0);
        layout.addView(choiceButton);
        layout.addView(statusView);
        layout.addView(explanation);
        scroll.addView(layout);
        setContentView(scroll);
    }

    private int indexOf(String choice) {
        for (int i = 0; i < VALUES.length; i++) if (VALUES[i].equals(choice)) return i;
        return 1; // Same conservative fallback as the service.
    }

    @Override public boolean onOptionsItemSelected(MenuItem item) {
        if (item.getItemId() == android.R.id.home) { finish(); return true; }
        return super.onOptionsItemSelected(item);
    }
    @Override public void onResume() { super.onResume(); resumed = true; refresh(); }
    @Override public void onPause() {
        resumed = false;
        handler.removeCallbacks(refresh);
        super.onPause();
    }
    @Override public void onDestroy() { reader.shutdownNow(); super.onDestroy(); }

    private void refresh() {
        if (!resumed || choiceButton == null) return;
        handler.removeCallbacks(refresh);
        String choice = getChoice();
        choiceButton.setText(getString(R.string.requested, labels[indexOf(choice)]));
        if (!reading) {
            reading = true;
            reader.execute(() -> {
                Map<String, String> state = new HashMap<>();
                try (FileInputStream stream = new FileInputStream(STATUS)) {
                    byte[] bytes = new byte[2048];
                    int length = stream.read(bytes);
                    if (length > 0 && length < bytes.length) {
                        for (String line : new String(bytes, 0, length, StandardCharsets.UTF_8).split("\n")) {
                            String[] parts = line.split("=", 2);
                            if (parts.length == 2) state.put(parts[0], parts[1]);
                        }
                    }
                } catch (Exception ignored) { /* A missing policy is displayed explicitly. */ }
                handler.post(() -> {
                    reading = false;
                    if (resumed && !isDestroyed()) render(state);
                });
            });
        }
        handler.postDelayed(refresh, 2000);
    }

    private void render(Map<String, String> state) {
        String choice = getChoice();
        String label = labels[indexOf(choice)];
        try {
            int requested = Integer.parseInt(state.get("requested_ua"));
            int applied = Integer.parseInt(state.get("applied_ua"));
            int error = Integer.parseInt(state.get("error"));
            int wanted = "auto".equals(choice) ? 1000000 : Integer.parseInt(choice) * 1000;
            if (error != 0) {
                statusView.setText(getString(reason(state.get("reason"))) + "\n"
                        + getString(R.string.io_error));
            } else if (requested != wanted || applied > requested) {
                statusView.setText(getString(R.string.pending, label));
            } else {
                statusView.setText(getString(R.string.applied, applied / 1000) + "\n"
                        + getString(reason(state.get("reason"))));
            }
        } catch (RuntimeException missing) {
            statusView.setText(getString(R.string.unavailable, label));
        }
    }

    private int reason(String value) {
        if (value == null) return R.string.waiting;
        switch (value) {
            case "ready": return R.string.ready;
            case "user-limit": return R.string.user_limit;
            case "thermal-limit": return R.string.thermal_limit;
            case "usb-budget": return R.string.usb_budget;
            case "temperature": return R.string.temperature;
            case "voltage": return R.string.voltage;
            case "unplugged": return R.string.unplugged;
            case "no-battery": return R.string.no_battery;
            case "sensor-error": return R.string.sensor_error;
            case "charger-fault": return R.string.charger_fault;
            case "safety-timer": return R.string.safety_timer;
            case "stale": return R.string.stale;
            case "stopped": return R.string.stopped;
            case "io-error": return R.string.io_error;
            default: return R.string.waiting;
        }
    }
}
