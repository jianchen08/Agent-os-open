package com.lingxi.agentos;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.Properties;

import org.junit.Test;

/**
 * ShellConfigResolver 决策表契约测试（本地 JVM 单测，纯函数零 Android 依赖）。
 * 覆盖：env-override 胜出 / 声明面兜底 / 坏 sidecar 与空 override 回声明面 /
 * 声明面缺失不拦截（null）/ sidecar 解析（UTF-8 注释行 / IO 失败 null）/
 * 注入脚本合成与 JSON 转义（脚本注入面防护）。
 */
public class ShellConfigResolverTest {

    private static final String CLOUD = "http://cloud.example";
    private static final String LOOPBACK = "http://127.0.0.1:9300";

    private static Properties sidecar(String source, String apiBase) {
        Properties p = new Properties();
        if (source != null) {
            p.setProperty("source", source);
        }
        if (apiBase != null) {
            p.setProperty("apiBase", apiBase);
        }
        return p;
    }

    @Test
    public void declaredFace_alone_wins() {
        assertEquals(CLOUD, ShellConfigResolver.resolveApiBase(CLOUD, null));
    }

    @Test
    public void declaredModeSidecar_fallsBackToDeclaredFace() {
        assertEquals(CLOUD,
                ShellConfigResolver.resolveApiBase(CLOUD, sidecar("declared", CLOUD)));
    }

    @Test
    public void envOverride_winsOverDeclaredFace() {
        // 双模式区分度组：声明面=云、override=回环，两值语义相反，override 必须胜出
        assertEquals(LOOPBACK,
                ShellConfigResolver.resolveApiBase(CLOUD, sidecar("env-override", LOOPBACK)));
    }

    @Test
    public void envOverride_withEmptyValue_ignored() {
        assertEquals(CLOUD,
                ShellConfigResolver.resolveApiBase(CLOUD, sidecar("env-override", "")));
    }

    @Test
    public void sidecar_withUnknownSource_ignored() {
        assertEquals(CLOUD,
                ShellConfigResolver.resolveApiBase(CLOUD, sidecar("typo-mode", LOOPBACK)));
        assertEquals(CLOUD, ShellConfigResolver.resolveApiBase(CLOUD, sidecar(null, LOOPBACK)));
    }

    @Test
    public void missingDeclaredFace_meansNoInterception() {
        assertNull(ShellConfigResolver.resolveApiBase(null, null));
        assertNull(ShellConfigResolver.resolveApiBase("", sidecar("declared", "")));
    }

    @Test
    public void parseSidecar_readsUtf8WithComments() {
        // syncweb 生成物实样：中文注释行 + 键值面；注释行不得毒化解析
        String text = "# 由 scripts/syncweb.mjs 生成，勿手改\n"
                + "# source=declared|env-override：构建缝双模式标注\n"
                + "apiBase=" + LOOPBACK + "\nsource=env-override\n";
        Properties p = ShellConfigResolver.parseSidecar(
                new ByteArrayInputStream(text.getBytes(java.nio.charset.StandardCharsets.UTF_8)));
        assertEquals(LOOPBACK, p.getProperty("apiBase"));
        assertEquals("env-override", p.getProperty("source"));
    }

    @Test
    public void parseSidecar_ioFailure_returnsNull() {
        InputStream broken = new InputStream() {
            @Override
            public int read() throws IOException {
                throw new IOException("broken stream");
            }
        };
        assertNull(ShellConfigResolver.parseSidecar(broken));
    }

    @Test
    public void render_matchesGeneratorFormat() {
        assertEquals(
                "window.LINGXI_SHELL_CONFIG = { apiBase: \"" + CLOUD
                        + "\", versionName: \"1.19\", versionCode: 20 };\n",
                ShellConfigResolver.renderShellConfigJs(CLOUD, "1.19", 20));
    }

    @Test
    public void render_escapesJsonSpecials_noScriptInjection() {
        String hostile = "http://x/\",versionName:\"pwned";
        String js = ShellConfigResolver.renderShellConfigJs(hostile, "1.0", 1);
        // 转义后整串仍是单个合法 JS 表达式行：值内不得出现未转义引号
        assertEquals(
                "window.LINGXI_SHELL_CONFIG = { apiBase: \"http://x/\\\",versionName:\\\"pwned"
                        + "\", versionName: \"1.0\", versionCode: 1 };\n",
                js);
    }
}
