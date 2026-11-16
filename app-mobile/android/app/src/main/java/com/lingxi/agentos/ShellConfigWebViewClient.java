package com.lingxi.agentos;

import android.content.Context;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.os.Build;
import android.util.Log;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;

import com.getcapacitor.Bridge;
import com.getcapacitor.BridgeWebViewClient;
import com.getcapacitor.CapConfig;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.Properties;

/**
 * 壳配置应答 WebViewClient（D1①）：对 /shell-config.js 的请求用声明面/sidecar
 * 决策值（ShellConfigResolver）现做现答，生成物文件的 apiBase 值不再被运行时
 * 消费——在库/包内生成物漂移（审查总册 P1-14②）对运行时免疫。
 *
 * 时序契约：MainActivity.load() 在 super.load()（bridge 建立、默认 client 已装、
 * 首导航已发起）后同一 UI 线程栈内换装本 client——shouldInterceptRequest 回调
 * 经 UI 线程消息派发，必晚于本次换装，首个请求即走本链，无竞态窗口。
 *
 * 声明面缺失（配置回归）时不拦截，生成物原样应答——降级不静默（Log 告警）；
 * 在库声明面由 scripts/check_shell_config_sync.py 闸执法。
 *
 * 安全注：apiBase 现为公网明文 HTTP（无 TLS 中间人面）——明文 HTTP 过渡态，
 * TLS 待立项（审查总册 P1-14③，需拍板节奏）。
 */
public class ShellConfigWebViewClient extends BridgeWebViewClient {

    private static final String TAG = "LingxiShellConfig";
    /** syncweb step4 注入的固定标签路径（www/index.html head 经典脚本，契约面） */
    private static final String SHELL_CONFIG_PATH = "/shell-config.js";
    /** sidecar 在包内位置（cap sync 把 www/ 整拷进 assets/public） */
    private static final String SIDECAR_ASSET = "public/shell-config.properties";

    private final Bridge bridge;

    public ShellConfigWebViewClient(Bridge bridge) {
        super(bridge);
        this.bridge = bridge;
    }

    @Override
    public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
        if (SHELL_CONFIG_PATH.equals(request.getUrl().getPath())) {
            WebResourceResponse injected = serveShellConfig();
            if (injected != null) {
                return injected;
            }
        }
        return super.shouldInterceptRequest(view, request);
    }

    /** 决策值合成注入脚本应答；null = 不拦截（声明面缺失回退生成物）。 */
    private WebResourceResponse serveShellConfig() {
        String apiBase = ShellConfigResolver.resolveApiBase(declaredApiBase(), readSidecar());
        if (apiBase == null) {
            Log.w(TAG, "declared apiBase missing (config regression); "
                    + "falling back to bundled shell-config.js");
            return null;
        }
        PackageInfo pkg = packageInfo();
        String versionName = pkg != null && pkg.versionName != null ? pkg.versionName : "";
        int versionCode = 0;
        if (pkg != null) {
            versionCode = Build.VERSION.SDK_INT >= 28
                    ? (int) pkg.getLongVersionCode()
                    : pkg.versionCode;
        }
        byte[] body = ShellConfigResolver
                .renderShellConfigJs(apiBase, versionName, versionCode)
                .getBytes(StandardCharsets.UTF_8);
        WebResourceResponse response = new WebResourceResponse(
                "application/javascript", "UTF-8", new ByteArrayInputStream(body));
        response.setStatusCodeAndReasonPhrase(200, "OK");
        return response;
    }

    /** 声明面：capacitor.config.json plugins.ShellConfig.apiBase（cap sync 随包，唯一真值）。 */
    private String declaredApiBase() {
        CapConfig config = bridge.getConfig();
        return config != null ? config.getPluginConfiguration("ShellConfig").getString("apiBase") : null;
    }

    /** sidecar（syncweb 生成链 env-override 通道）；缺文件/IO 失败 = null（按声明面兜底）。 */
    private Properties readSidecar() {
        Context ctx = bridge.getContext();
        if (ctx == null) {
            return null;
        }
        try (InputStream in = ctx.getAssets().open(SIDECAR_ASSET)) {
            return ShellConfigResolver.parseSidecar(in);
        } catch (IOException e) {
            return null;
        }
    }

    private PackageInfo packageInfo() {
        Context ctx = bridge.getContext();
        if (ctx == null) {
            return null;
        }
        try {
            return ctx.getPackageManager().getPackageInfo(ctx.getPackageName(), 0);
        } catch (PackageManager.NameNotFoundException e) {
            Log.w(TAG, "package info unavailable: " + e);
            return null;
        }
    }
}
