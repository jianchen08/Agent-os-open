package com.lingxi.agentos;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/**
 * 本机档登录凭据失败看门狗契约测试（S5-A③，本地 JVM 单测，纯 Java 零 Android 依赖）。
 *
 * 计数契约双闸：①武装闸——内核 /health 首个 200（arm()）之前任何失败不累积
 * 不触发；②类别闸——仅凭据类进连续计数，可用性类不计数不清零。
 * 主断言（任务书口径）：真凭据连续 ≥3 触发一次重播种（N=3 不变）；防死循环=
 * 重播种至多一轮/漂移 episode（触发即闩锁，成功登录才复位）；「连续」语义=
 * 成功登录打断计数。
 */
public class ShellAuthWatchdogTest {

    private static final ShellAuthWatchdog.FailureClass CRED =
            ShellAuthWatchdog.FailureClass.CREDENTIAL;
    private static final ShellAuthWatchdog.FailureClass AVAIL =
            ShellAuthWatchdog.FailureClass.AVAILABILITY;

    @Test
    public void thresholdPin_isThree() {
        // 任务书固定 N=3：契约钉住（阈值漂移=自愈灵敏度/误触发面的行为变更）
        assertEquals(3, ShellAuthWatchdog.RESEED_THRESHOLD);
    }

    // ── 武装闸（内核 /health 首个 200 前不武装） ──────────────────────────

    @Test
    public void unArmed_credentialFailures_neverTriggerReseed() {
        // 慢 boot（注册期连接失败/boot 窗口响应）攒满阈值打杀健康内核树的假
        // reseed 根除：武装前凭据类失败也不触发
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
    }

    @Test
    public void unArmed_failures_doNotAccumulate_afterArming() {
        // 武装前失败不累积：武装后须重新攒满 3 次真凭据错才触发
        // （区分度反例：若武装前暗中累积，此处第 3 次 onAuthFailure 即触发）
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.onAuthFailure(CRED);
        w.onAuthFailure(CRED);
        w.arm();
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        assertTrue(w.onAuthFailure(CRED));
    }

    @Test
    public void arm_isIdempotentAndMonotonic() {
        // 首个 200 即武装点：幂等无副作用；无 disarm 面（单调不复位）
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        assertFalse(w.isArmed());
        w.arm();
        assertTrue(w.isArmed());
        w.arm();
        assertTrue(w.isArmed());
    }

    // ── 类别闸（仅凭据类计数） ────────────────────────────────────────────

    @Test
    public void credential_x3_triggersReseedOnce() {
        // 原初用途保留：真凭据连续 3 错 → 一次重播种（N=3 不变）
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.arm();
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        assertTrue(w.onAuthFailure(CRED));
        // 闩锁：episode 内重复失败不再触发（重播种无效的环境不空转重启）
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
    }

    @Test
    public void availability_xN_neverCounts_neverTriggers() {
        // 连接拒绝/超时/5xx：不可用≠凭据错，恒不触发
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.arm();
        for (int i = 0; i < 10; i++) {
            assertFalse(w.onAuthFailure(AVAIL));
        }
        // 区分度：可用性失败未计入（若计入，下一发凭据错即触发）
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        assertTrue(w.onAuthFailure(CRED));
    }

    @Test
    public void mixedSequence_onlyCredentialCounts() {
        // 混合序列：凭据×2 + 可用性×N + 凭据×1 → 第 3 次凭据错触发；
        // 可用性既不计数也不打断「连续」凭据链（不可用不是清白证据）
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.arm();
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        for (int i = 0; i < 5; i++) {
            assertFalse(w.onAuthFailure(AVAIL));
        }
        assertTrue(w.onAuthFailure(CRED));
    }

    // ── 成功复位（连续语义 + 闩锁复位） ──────────────────────────────────

    @Test
    public void success_breaksConsecutiveCount() {
        // 两次失败+成功+两次失败：连续性被打断，不触发（无区分度反例：
        // 若计数不因成功清零，此处会累计到 4 误触发）
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.arm();
        w.onAuthFailure(CRED);
        w.onAuthFailure(CRED);
        w.onAuthSuccess();
        assertFalse(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
    }

    @Test
    public void success_afterReseed_opensNewEpisode() {
        // 两次重播种之间必有成功登录（自愈达成的证据），死循环不成立：
        // 重播种 → 失败不再触发 → 成功复位 → 新漂移可再次触发
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.arm();
        w.onAuthFailure(CRED);
        w.onAuthFailure(CRED);
        assertTrue(w.onAuthFailure(CRED));
        assertFalse(w.onAuthFailure(CRED));
        w.onAuthSuccess();
        w.onAuthFailure(CRED);
        w.onAuthFailure(CRED);
        assertTrue(w.onAuthFailure(CRED));
    }

    @Test
    public void success_alone_resetsLatchedEpisode() {
        ShellAuthWatchdog w = new ShellAuthWatchdog();
        w.arm();
        w.onAuthFailure(CRED);
        w.onAuthFailure(CRED);
        w.onAuthFailure(CRED);
        w.onAuthSuccess();
        // 复位后计数从零起算，单次失败不触发
        assertFalse(w.onAuthFailure(CRED));
    }
}
