package com.lingxi.agentos;

import static org.junit.Assert.assertEquals;

import java.util.List;

import org.junit.Test;

/**
 * 访客内核凭据 env 契约测试（本地 JVM 单测，纯 Java 零 Android 依赖）。
 *
 * 契约核心：变量名与内核消费面一字不差。2026-10-22 实证密钥曾以 TOKEN_SECRET
 * 名注入（内核只认 AGENTOS_TOKEN_SECRET），静默丢弃 → 进程随机签名 → 重启
 * 全量会话失效；本测试锁死两个名字防名字漂移复发。
 */
public class KernelGuestEnvTest {

    @Test
    public void entries_useKernelConsumedNames_inOrder() {
        List<String> entries = KernelGuestEnv.credentialEntries("device-pw", "device-secret");
        assertEquals(2, entries.size());
        assertEquals("AGENTOS_ADMIN_PASSWORD=device-pw", entries.get(0));
        assertEquals("AGENTOS_TOKEN_SECRET=device-secret", entries.get(1));
    }

    @Test
    public void emptySecret_passesThrough_failOpenToKernelRandom() {
        // 空密钥不放大为起栈失败：内核侧空白值回落进程随机签名（agentos_http::auth
        // 空白分支），缺密钥的代价是回退旧行为而非起不来
        List<String> entries = KernelGuestEnv.credentialEntries("device-pw", "");
        assertEquals("AGENTOS_TOKEN_SECRET=", entries.get(1));
    }
}
