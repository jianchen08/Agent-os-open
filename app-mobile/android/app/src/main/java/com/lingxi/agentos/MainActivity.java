package com.lingxi.agentos;

import android.graphics.Color;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.view.Window;
import android.webkit.WebView;
import android.util.Log;

import androidx.core.graphics.Insets;
import androidx.core.view.ViewCompat;
import androidx.core.view.WindowCompat;
import androidx.core.view.WindowInsetsCompat;
import androidx.activity.OnBackPressedCallback;

import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeActivity;
import com.getcapacitor.WebViewListener;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import org.json.JSONException;
import org.json.JSONObject;

/**
 * 纯 WebView 壳：页面加载完成后注入壳层轮询脚本（shell-push.js）+ 同步模块
 * （shell-sync.js，源单点 src/sync/ 由 scripts/build-sync.mjs 生成）+
 * 系统栏 insets CSS 变量补写 + 最近主题播种（SharedPreferences 单源，键名见
 * ShellIntentsPlugin；恢复判定在前端 initializeTheme）。通知/保活走成熟插件栈
 * （@capacitor/local-notifications + @capawesome-team/capacitor-android-foreground-service），
 * 轮询与去重逻辑在壳 JS 内，零前端改动。
 */
public class MainActivity extends BridgeActivity {

    private static final String TAG = "LingxiShell";

    /** 最近一次 systemBars+displayCutout insets；页面加载完成时补写 CSS 变量用 */
    private Insets lastBarInsets = Insets.NONE;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // 壳自建插件须在 super.onCreate（Bridge 初始化）前注册
        registerPlugin(ShellIntentsPlugin.class);
        registerPlugin(KernelHostPlugin.class);
        registerPlugin(ShellSpeechPlugin.class);
        super.onCreate(savedInstanceState);
        configureImmersiveWindow();
        exposeSystemBarInsetsToPage();
        injectShellPushScriptOnPageLoaded();
        // 硬件返回键 = WebView 历史回退；无历史则走默认行为退出应用。
        // Capacitor 8 核心不再内置返回键处理（App 插件已移出核心包），由壳自行实现。
        // P1-8 浮层优先语义（移动端视觉优化批，MuMu 实测：浮窗/popover 打开时
        // 按 back 直接回退/退出）：回退/退出之前先问页面——派发 lingxi-back-press
        // 事件给前端协调器（androidBackBridge），detail.consumed=true（浮层已关）
        // 则本按消费不回退；页面无浮层/无协调器（consumed=false）回退既有语义，
        // 行为与改前完全一致。evaluateJavascript 派发是同步遍历监听器（监听器
        // 同步置位 consumed），结果经回调回 UI 线程裁决。
        getOnBackPressedDispatcher().addCallback(this, new OnBackPressedCallback(true) {
            @Override
            public void handleOnBackPressed() {
                Bridge bridge = MainActivity.this.bridge;
                WebView webView = bridge != null ? bridge.getWebView() : null;
                if (webView == null) {
                    setEnabled(false);
                    getOnBackPressedDispatcher().onBackPressed();
                    return;
                }
                final WebView wv = webView;
                webView.evaluateJavascript(
                        "(function(){try{"
                                + "var d={consumed:false};"
                                + "window.dispatchEvent(new CustomEvent('lingxi-back-press',{detail:d}));"
                                + "return d.consumed?'consumed':'pass';"
                                + "}catch(e){return 'pass';}})()",
                        result -> {
                            boolean consumed = result != null && result.contains("consumed");
                            if (consumed) {
                                return; // 浮层已关，本按不回退
                            }
                            // 未消费：既有语义（历史回退 → 无历史退出）
                            wv.post(() -> {
                                if (wv.canGoBack()) {
                                    wv.goBack();
                                } else {
                                    setEnabled(false);
                                    getOnBackPressedDispatcher().onBackPressed();
                                }
                            });
                        });
            }
        });
    }

    /**
     * 壳配置应答 client 换装（D1①）：super.load()（bridge 建立 + 默认 client 装
     * 配 + 首导航发起）后同一 UI 线程栈内换 ShellConfigWebViewClient——拦截回调
     * 经 UI 线程消息派发必晚于本次换装，首个 /shell-config.js 请求即走声明面
     * 决策链（ShellConfigResolver），生成物漂移对运行时免疫。时序契约详见该类。
     */
    @Override
    protected void load() {
        super.load();
        Bridge bridge = this.bridge;
        if (bridge == null) {
            return;
        }
        bridge.setWebViewClient(new ShellConfigWebViewClient(bridge));
        if (bridge.getWebView() != null) {
            bridge.getWebView().setWebViewClient(bridge.getWebViewClient());
        }
    }

    @Override
    public void onStart() {
        super.onStart();
        // 前台活跃：停 FGS（保活通知只在退后台/息屏时有价值，状态栏保持干净）
        KernelHostPlugin.onHostActivityForeground();
    }

    @Override
    public void onStop() {
        super.onStop();
        // 退后台/息屏：local 模式且本机栈在跑且用户未手动停止 → 起 FGS 保活
        KernelHostPlugin.onHostActivityBackground();
    }

    /**
     * 全屏沉浸基座（用户裁定 2026-10-13）：窗口铺到系统栏底下，内容背景直通
     * 状态栏/手势条区域。Android 15+（targetSdk 36 强制 edge-to-edge）天然全屏；
     * Android 14 及以下默认 decor 收下 systemBars insets 并把内容排在系统栏
     * 下方（insets 到不了 WebView，全屏不成立），须显式关掉 decor fitting 并把
     * 系统栏置透明（15+ 颜色 API 被忽略、天然透明，此调用幂等）。
     */
    private void configureImmersiveWindow() {
        Window window = getWindow();
        WindowCompat.setDecorFitsSystemWindows(window, false);
        window.setStatusBarColor(Color.TRANSPARENT);
        window.setNavigationBarColor(Color.TRANSPARENT);
    }

    /**
     * 系统栏遮挡避让数据源 + 键盘垫高：WebView 不打系统栏 padding（内容铺到
     * 栏底下），壳把 systemBars+displayCutout insets 写入根节点 CSS 变量
     * safe-inset-top/bottom，前端顶栏取 max(env(safe-area-inset-*), 变量值)
     * ——Android WebView 的 env() 恒 0（Chromium 140 起才透传真值），注入
     * 变量是 Android 侧有效来源；桌面浏览器两者皆 0 零影响。键盘（ime）
     * 弹出时 WebView 底部垫高到键盘上沿、bottom 变量清零（输入区已可视，
     * 无需再留手势条位）。Capacitor 内置 SystemBars 的 insets 处理已按
     * 配置关闭（其 decor 垫高会把整页排到系统栏下方，全屏不成立），insets
     * 数据源由本壳单点承担。
     */
    private void exposeSystemBarInsetsToPage() {
        Bridge bridge = this.bridge;
        if (bridge == null) {
            return;
        }
        WebView webView = bridge.getWebView();
        if (webView == null) {
            return;
        }
        ViewCompat.setOnApplyWindowInsetsListener(webView, (view, windowInsets) -> {
            Insets bars = windowInsets.getInsets(
                    WindowInsetsCompat.Type.systemBars()
                            | WindowInsetsCompat.Type.displayCutout());
            boolean keyboardVisible = windowInsets.isVisible(WindowInsetsCompat.Type.ime());
            Insets ime = windowInsets.getInsets(WindowInsetsCompat.Type.ime());
            view.setPadding(0, 0, 0, keyboardVisible ? ime.bottom : 0);
            lastBarInsets = keyboardVisible
                    ? Insets.of(bars.left, bars.top, bars.right, 0)
                    : bars;
            Log.d(TAG, "insets dispatch: top=" + bars.top + " bottom=" + bars.bottom
                    + " left=" + bars.left + " right=" + bars.right
                    + " keyboard=" + keyboardVisible);
            applyBarInsetsToPage(webView, lastBarInsets);
            return WindowInsetsCompat.CONSUMED;
        });
    }

    /** CSS 变量取 CSS px（insets 是物理像素，须除 density；与 Capacitor
     * SystemBars.injectSafeAreaCSS 同一口径），键盘垫高 view.setPadding
     * 才用物理像素。 */
    private void applyBarInsetsToPage(WebView webView, Insets bars) {
        float density = getResources().getDisplayMetrics().density;
        String js = "document.documentElement.style.setProperty('--safe-inset-top','"
                + Math.round(bars.top / density) + "px');"
                + "document.documentElement.style.setProperty('--safe-inset-bottom','"
                + Math.round(bars.bottom / density) + "px');";
        webView.evaluateJavascript(js, null);
    }

    /**
     * 页面→壳播种（putIfAbsent 语义）：每次页面加载完成从 localStorage 拉当前
     * 生效主题 id，仅在壳持久化尚无值时落库——给 v1.9 升级用户补种初始值
     * （其切换后主题经 ShellIntents.notifyShellTheme 桥上报覆盖）。只播种不
     * 覆盖：页面 persist 对空 storage 会写回默认值，与用户真实选择不可区分，
     * 无条件覆盖会把恢复数据源（前端启动查询 getLastTheme 消费）毒化成默认
     * 浅色。主题恢复的判定与执行在前端 initializeTheme（模块导入时快照
     * storage 是否有键——壳 onPageLoaded 检查键缺失必输给 persist 首次写回，
     * 实测竞态已证）。
     */
    private void seedLastThemeFromPageIfAbsent(WebView webView) {
        String script = "(function(){try{"
                + "var s=JSON.parse(localStorage.getItem('theme-storage')||'{}');"
                + "return (s&&s.state&&typeof s.state.currentThemeId==='string')"
                + "?{id:s.state.currentThemeId}:{}"
                + "}catch(e){return{}}})()";
        webView.evaluateJavascript(script, result -> {
            if (result == null) {
                return;
            }
            try {
                String id = new JSONObject(result).optString("id", "");
                if (id.isEmpty()) {
                    return;
                }
                SharedPreferences prefs =
                        getSharedPreferences(ShellIntentsPlugin.PREFS_NAME, MODE_PRIVATE);
                if (!prefs.contains(ShellIntentsPlugin.KEY_LAST_THEME_ID)) {
                    prefs.edit().putString(ShellIntentsPlugin.KEY_LAST_THEME_ID, id).apply();
                }
            } catch (JSONException e) {
                Log.w(TAG, "seed last theme failed: " + e);
            }
        });
    }

    /** 每次页面加载完成注入一次壳层脚本；SPA 站内路由不重触发，同一文档内定时器持续有效。 */
    private void injectShellPushScriptOnPageLoaded() {
        Bridge bridge = this.bridge;
        if (bridge == null) {
            return;
        }
        bridge.addWebViewListener(new WebViewListener() {
            @Override
            public void onPageLoaded(WebView webView) {
                // 每次页面加载完成都补写 CSS 变量（WebView 重载/重建成后文档必不丢）。
                // 无值（监听器尚未派发）时写 0px——让「壳已写 0」与「壳未写（空串）」
                // 在前端诊断面板可辨别，白条/挤开归因不歧义。
                applyBarInsetsToPage(webView, lastBarInsets);
                seedLastThemeFromPageIfAbsent(webView);
                String script = readAsset("shell-push.js");
                if (script == null) {
                    return;
                }
                webView.evaluateJavascript(script, null);
                Log.d(TAG, "shell-push.js injected");
                String syncScript = readAsset("shell-sync.js");
                if (syncScript != null) {
                    webView.evaluateJavascript(syncScript, null);
                    Log.d(TAG, "shell-sync.js injected");
                }
            }
        });
    }

    private String readAsset(String name) {
        InputStream in = null;
        try {
            in = getAssets().open(name);
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            byte[] buf = new byte[8192];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
            return new String(out.toByteArray(), StandardCharsets.UTF_8);
        } catch (IOException e) {
            Log.w(TAG, "read asset failed: " + name + " - " + e);
            return null;
        } finally {
            if (in != null) {
                try {
                    in.close();
                } catch (IOException ignored) {
                    // 关闭失败无业务影响
                }
            }
        }
    }
}
