package com.lingxi.agentos;

import java.util.function.Supplier;

/**
 * 壳侧凭据非空防御（纯逻辑，JVM 可测）：prootCommand 拼 AGENTOS_ADMIN_PASSWORD
 * 前调用——任何时序下不得把空串传给内核。空口令触发内核自生成分支
 * （boot_env.rs 空值自生成随机口令）=口令漂移源，壳凭据自此恒败。
 *
 * 契约：非空原样透传且零补种（健康路径零副作用）；空 → 先走补种
 * （ensureCredentials）再读；补种后仍空 → null，调用方 fail-closed 不起栈
 * （宁可起不来，不制造漂移）。
 */
public final class ShellCredentialGuard {

    private final Supplier<String> read;
    private final Runnable ensure;

    public ShellCredentialGuard(Supplier<String> read, Runnable ensure) {
        this.read = read;
        this.ensure = ensure;
    }

    /** @return 非空口令；补种后仍空返回 null（调用方 fail-closed） */
    public String requirePassword() {
        String password = read.get();
        if (isEmpty(password)) {
            ensure.run();
            password = read.get();
        }
        return isEmpty(password) ? null : password;
    }

    private static boolean isEmpty(String s) {
        return s == null || s.isEmpty();
    }
}
