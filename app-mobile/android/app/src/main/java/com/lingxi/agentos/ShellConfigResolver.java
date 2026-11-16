package com.lingxi.agentos;

import java.io.IOException;
import java.io.InputStream;
import java.io.InputStreamReader;
import java.nio.charset.StandardCharsets;
import java.util.Properties;

/**
 * 壳运行时 apiBase 决策单点（D1①，纯 Java 零 Android 依赖，JUnit 直测）。
 *
 * 值源优先级：
 *   1. sidecar（assets/public/shell-config.properties，syncweb 生成）且
 *      source=env-override 且 apiBase 非空 → 构建方 LINGXI_API_BASE 注入的
 *      本机内核回环（此包的声明面此时仍是云端真值，env-override 键是双模式
 *      G-M0 唯一的可辨别通道）；
 *   2. 否则 → capacitor.config.json 声明面 plugins.ShellConfig.apiBase
 *      （declared face，钉死）；sidecar 缺失/损坏 = 旧生成链产物，同样按
 *      声明面兜底；
 *   3. 声明面缺失/空 → null（调用方不拦截 /shell-config.js，生成物原样应答
 *      兜底 + 告警——降级不静默；声明面在库由 scripts/check_shell_config_sync.py
 *      闸执法，运行时此分支=配置回归的最后防线而非静默错值）。
 *
 * 生成物 www/shell-config.js 的 apiBase 值因此不再被运行时消费——该文件的
 * 在库漂移（审查总册 P1-14②）由闸执法，运行时错误值被本决策链免疫。
 *
 * 安全注：apiBase 现为公网明文 HTTP（无 TLS 中间人面）——明文 HTTP 过渡态，
 * TLS 待立项（审查总册 P1-14③，需拍板节奏），本类如实保留现状。
 */
public final class ShellConfigResolver {

    /** sidecar source=env-override 键值（syncweb 生成链契约，勿单方改动） */
    public static final String SOURCE_ENV_OVERRIDE = "env-override";

    private ShellConfigResolver() {
    }

    /**
     * 决策：应答 /shell-config.js 时使用的 apiBase；null = 不拦截（回退生成物）。
     *
     * @param declaredApiBase 声明面值（null/空 = 配置回归）
     * @param sidecar         sidecar 键值面（null = 旧生成链产物/读取失败）
     */
    public static String resolveApiBase(String declaredApiBase, Properties sidecar) {
        if (sidecar != null
                && SOURCE_ENV_OVERRIDE.equals(sidecar.getProperty("source"))
                && !isEmpty(sidecar.getProperty("apiBase"))) {
            return sidecar.getProperty("apiBase");
        }
        return isEmpty(declaredApiBase) ? null : declaredApiBase;
    }

    /**
     * 解析 sidecar 字节流（UTF-8 Properties）。解析失败按 null 兜底——损坏文件
     * 不阻塞壳启动，回退声明面。流由调用方关闭（原生侧 asset 流生命周期归主调）。
     */
    public static Properties parseSidecar(InputStream in) {
        try {
            Properties props = new Properties();
            props.load(new InputStreamReader(in, StandardCharsets.UTF_8));
            return props;
        } catch (IOException e) {
            return null;
        }
    }

    /**
     * 合成注入脚本（冻结格式，与 syncweb 生成物同形）：head 经典脚本执行时置
     * window.LINGXI_SHELL_CONFIG，先于前端 deferred 模块脚本的基址读取（时序有
     * 规范保证）。值经 JSON 字符串转义，禁裸拼——apiBase/versionName 来自配置
     * 面，拼接注入即脚本注入面。
     */
    public static String renderShellConfigJs(String apiBase, String versionName, int versionCode) {
        return "window.LINGXI_SHELL_CONFIG = { apiBase: \"" + jsonEscape(apiBase)
                + "\", versionName: \"" + jsonEscape(versionName)
                + "\", versionCode: " + versionCode + " };\n";
    }

    /** RFC 8259 字符串转义最小集（引号/反斜杠/控制字符；输入为配置面 URL 与版本号）。 */
    private static String jsonEscape(String s) {
        StringBuilder sb = new StringBuilder(s.length() + 2);
        for (int i = 0; i < s.length(); i++) {
            char c = s.charAt(i);
            switch (c) {
                case '\\':
                    sb.append("\\\\");
                    break;
                case '"':
                    sb.append("\\\"");
                    break;
                case '\n':
                    sb.append("\\n");
                    break;
                case '\r':
                    sb.append("\\r");
                    break;
                case '\t':
                    sb.append("\\t");
                    break;
                default:
                    if (c < 0x20) {
                        sb.append(String.format("\\u%04x", (int) c));
                    } else {
                        sb.append(c);
                    }
            }
        }
        return sb.toString();
    }

    private static boolean isEmpty(String s) {
        return s == null || s.isEmpty();
    }
}
