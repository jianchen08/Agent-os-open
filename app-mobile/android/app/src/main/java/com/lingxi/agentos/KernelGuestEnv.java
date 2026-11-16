package com.lingxi.agentos;

import java.util.Arrays;
import java.util.List;

/**
 * 访客内核凭据 env 键值对（prootCommand 尾段拼装消费，纯 Java 零 Android 依赖
 * 便于 JVM 单测，协作类先例=ShellCredentialGuard）。
 *
 * 契约：变量名与内核消费面一字不差——admin 口令=AGENTOS_ADMIN_PASSWORD
 * （boot_env.rs seed_admin_user），token 签名密钥=AGENTOS_TOKEN_SECRET
 * （agentos_http::auth TOKEN_SECRET_ENV）。2026-10-22 实证：密钥曾以
 * TOKEN_SECRET 名注入，内核静默丢弃回落进程随机签名，重启即全量会话失效
 * （冷启动/切换连接往返均要求重登，restart_recovery 会话连续性失效）。
 * 空密钥原样透传：内核侧空白值回落进程随机，不把密钥缺失放大为起栈失败。
 */
public final class KernelGuestEnv {

    private KernelGuestEnv() {
    }

    /** 凭据 env 键值对（顺序固定：admin 口令、token 签名密钥） */
    public static List<String> credentialEntries(String adminPassword, String tokenSecret) {
        return Arrays.asList(
                "AGENTOS_ADMIN_PASSWORD=" + adminPassword,
                "AGENTOS_TOKEN_SECRET=" + tokenSecret);
    }
}
