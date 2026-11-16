package com.lingxi.agentos;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertNull;
import static org.junit.Assert.assertTrue;

import java.util.concurrent.atomic.AtomicInteger;

import org.junit.Test;

/**
 * 壳侧凭据非空防御契约测试（S5-A③ 协调方补充件，本地 JVM 单测，纯 Java 零
 * Android 依赖）。
 *
 * 防御对象：prootCommand 拼 AGENTOS_ADMIN_PASSWORD 时任何时序下读到空串——
 * 空口令传给内核会触发其自生成分支（boot_env.rs 空值分支自生成随机口令），
 * 壳凭据自此恒败=漂移源。契约：空 → 先补种再读；补种后仍空 → null
 * （调用方 fail-closed 不起栈）；非空 → 原样透传且零补种（健康路径零副作用）。
 */
public class ShellCredentialGuardTest {

    @Test
    public void nonEmpty_passwordPassedThrough_ensureNotCalled() {
        // 健康路径零副作用：已有口令不触发补种
        AtomicInteger ensured = new AtomicInteger();
        ShellCredentialGuard guard = new ShellCredentialGuard(
                () -> "device-generated-pw", ensured::incrementAndGet);
        assertEquals("device-generated-pw", guard.requirePassword());
        assertTrue(ensured.get() == 0);
    }

    @Test
    public void empty_ensureFills_returnsFilled() {
        // 时序洞补种路径：首读空 → 补种（ensureCredentials）→ 复读拿到生成口令
        AtomicInteger ensured = new AtomicInteger();
        AtomicInteger reads = new AtomicInteger();
        ShellCredentialGuard guard = new ShellCredentialGuard(
                () -> reads.incrementAndGet() == 1 ? "" : "generated-after-ensure",
                ensured::incrementAndGet);
        assertEquals("generated-after-ensure", guard.requirePassword());
        assertTrue("空读必须恰好补种一次", ensured.get() == 1);
    }

    @Test
    public void empty_ensureNoOp_returnsNull_failClosed() {
        // 补种也救不回（prefs 写失败等）：fail-closed 返回 null，调用方必须不起栈
        ShellCredentialGuard guard = new ShellCredentialGuard(() -> "", () -> {});
        assertNull(guard.requirePassword());
    }

    @Test
    public void nullPassword_treatedAsEmpty() {
        ShellCredentialGuard guard = new ShellCredentialGuard(
                () -> null, () -> {});
        assertNull(guard.requirePassword());
    }
}
