package com.lingxi.agentos;

import android.app.Activity;
import android.content.Intent;
import android.content.SharedPreferences;
import android.provider.Settings;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * 壳自建最小插件：系统通知设置跳转 + 最近主题持久化上报。
 *
 * 通知面：ACTION_APP_NOTIFICATION_SETTINGS + EXTRA_APP_PACKAGE（包名 extras）即
 * Android 8+ 的应用通知设置标准入口（通知总开关/悬浮通知/横幅同页）。
 *
 * 主题面：前端主题切换经 notifyShellTheme 把生效主题 id 写入应用私有
 * SharedPreferences（WebView localStorage 与 SharedPreferences 生命周期不同源：
 * WebView 存储被清/换源孤儿化时主题丢失，SharedPreferences 存活即可恢复，
 * 回写逻辑在 MainActivity 页面加载完成钩子）。读取方为 MainActivity，键名
 * 常量唯一来源在本类。
 */
@CapacitorPlugin(name = "ShellIntents")
public class ShellIntentsPlugin extends Plugin {

    /** 壳持久化文件与最近主题 id 键（MainActivity 恢复路径同源引用） */
    public static final String PREFS_NAME = "lingxi_shell";
    public static final String KEY_LAST_THEME_ID = "last_theme_id";

    @PluginMethod
    public void openNotificationSettings(PluginCall call) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("activity unavailable");
            return;
        }
        try {
            Intent intent = new Intent(Settings.ACTION_APP_NOTIFICATION_SETTINGS);
            intent.putExtra(Settings.EXTRA_APP_PACKAGE, activity.getPackageName());
            activity.startActivity(intent);
            call.resolve();
        } catch (Exception e) {
            call.reject("start APP_NOTIFICATION_SETTINGS failed", e);
        }
    }

    /** 最近主题 id 上报（前端 themeStore 订阅单点调用；浏览器/桌面无此桥） */
    @PluginMethod
    public void notifyShellTheme(PluginCall call) {
        String themeId = call.getString("themeId");
        if (themeId == null || themeId.isEmpty()) {
            call.reject("themeId required");
            return;
        }
        SharedPreferences prefs = getContext()
                .getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
        prefs.edit().putString(KEY_LAST_THEME_ID, themeId).apply();
        call.resolve();
    }

    /** 最近主题 id 查询（前端启动检测到 storage 无主题键时的恢复数据源；
     * 无存值返回空串，调用方跳过恢复） */
    @PluginMethod
    public void getLastTheme(PluginCall call) {
        SharedPreferences prefs = getContext()
                .getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
        String id = prefs.getString(KEY_LAST_THEME_ID, "");
        JSObject data = new JSObject();
        data.put("themeId", id == null ? "" : id);
        call.resolve(data);
    }

    /** 电池优化白名单查询（PowerManager 标准判定）。 */
    @PluginMethod
    public void isIgnoringBatteryOptimizations(PluginCall call) {
        android.os.PowerManager pm = (android.os.PowerManager)
                getContext().getSystemService(android.content.Context.POWER_SERVICE);
        JSObject data = new JSObject();
        data.put("ignoring", pm != null
                && pm.isIgnoringBatteryOptimizations(getContext().getPackageName()));
        call.resolve(data);
    }

    /** 电池优化白名单页（系统列表页路由，免 REQUEST_IGNORE_BATTERY_OPTIMIZATIONS 权限）。 */
    @PluginMethod
    public void openBatteryOptimizationSettings(PluginCall call) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("activity unavailable");
            return;
        }
        try {
            activity.startActivity(new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS));
            call.resolve();
        } catch (Exception e) {
            call.reject("start IGNORE_BATTERY_OPTIMIZATION_SETTINGS failed", e);
        }
    }

    /** 厂商自启管理页跳转（known-components best-effort 链，清单见
     * docs/working/移动端保活与厂商自启适配清单_20261014.md；全部未命中 reject）。 */
    @PluginMethod
    public void openVendorAutostartSettings(PluginCall call) {
        Activity activity = getActivity();
        if (activity == null) {
            call.reject("activity unavailable");
            return;
        }
        String[][] candidates = {
                {"com.miui.securitycenter", "com.miui.permcenter.autostart.AutoStartManagementActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity"},
                {"com.huawei.systemmanager", "com.huawei.systemmanager.appcontrol.activity.StartupAppControlActivity"},
                {"com.coloros.safecenter", "com.coloros.safecenter.permission.startup.StartupAppListActivity"},
                {"com.oppo.safe", "com.oppo.safe.permission.startup.StartupAppListActivity"},
                {"com.iqoo.secure", "com.iqoo.secure.ui.phoneoptimize.BgStartUpManager"},
                {"com.vivo.permissionmanager", "com.vivo.permissionmanager.activity.BgStartUpManagerActivity"},
                {"com.samsung.android.lool", "com.samsung.android.sm.ui.ram.AutoRunActivity"},
                {"com.meizu.safe", "com.meizu.safe.security.SHOW_APPSEC"},
        };
        for (String[] c : candidates) {
            try {
                Intent intent = new Intent();
                intent.setClassName(c[0], c[1]);
                activity.startActivity(intent);
                JSObject data = new JSObject();
                data.put("opened", c[0]);
                call.resolve(data);
                return;
            } catch (Exception ignored) {
                // 组件不存在=该厂商不匹配，继续试下一家
            }
        }
        call.reject("no known vendor autostart component on this device");
    }
}
