package com.lingxi.agentos;

/**
 * 本机档登录凭据失败看门狗（S5-A③ 漂移自愈计数面）。
 *
 * 计数契约双闸（任一不满足不计）：
 * ①武装闸：内核 /health 首个 200（arm()）之前整体不武装——内核 boot 期间
 *   连接拒绝/超时与 boot 窗口响应都不是凭据证据（不可用≠凭据错），任何失败
 *   不累积不触发；首个 200 即武装点，武装单调不复位（无 disarm 面）。
 * ②类别闸：仅凭据类（CREDENTIAL）进连续计数；可用性类（AVAILABILITY）不
 *   计数不清零。类别由调用点在 HTTP 状态可见面判定后传入（KernelHostPlugin
 *   reportLoginOutcome 消费前端 axios 层分类，本内核凭据拒绝族=400
 *   BAD_REQUEST「用户名或密码错误」+ 401/403）；429 限速锁在调用面不上报
 *   （终态非凭据证据）。
 *
 * 达阈值触发一次重播种：内核重启即按 AGENTOS_ADMIN_PASSWORD env 重置口令
 * （boot_env.rs seed_admin_user 每靴重置语义），漂移与进程内限速表一并清除
 * （auth.rs 限流「重启清零」）。
 *
 * 防死循环契约：重播种至多一轮/漂移 episode——触发即闩锁，直到一次成功登录
 * （自愈达成的证据）才复位；闩锁期重复失败不再触发。两次重播种之间必有成功
 * 登录，循环不可能闭合；武装前闩锁不可能置位（计数门前置武装闸）。
 * 远端模式门槛由调用方承担（KernelHostPlugin isRemoteMode：远端口令错与
 * 本地栈无关，重播种无意义），本类不持上下文。
 * 纯 JVM 零 Android 依赖（SeedLayoutTest 同族可测）。
 */
public class ShellAuthWatchdog {

    /** 登录失败类别（调用点按 HTTP 状态可见面判定后传入）。 */
    public enum FailureClass {
        /** 服务端明确拒绝凭据：内核「用户名或密码错误」400 BAD_REQUEST 族 + 401/403。 */
        CREDENTIAL,
        /** 内核不可用：连接拒绝/超时/5xx——不可用≠凭据错，不计数不清零。 */
        AVAILABILITY
    }

    /** 连续凭据失败触发阈值（任务书口径 N=3）。 */
    static final int RESEED_THRESHOLD = 3;

    private int consecutiveFailures = 0;
    private boolean reseedLatched = false;
    private boolean armed = false;

    /** 武装（内核 /health 首个 200 到达时调用）：幂等，单调不复位。 */
    public synchronized void arm() {
        armed = true;
    }

    public synchronized boolean isArmed() {
        return armed;
    }

    /**
     * @param failureClass 失败类别（凭据/可用性），见类注计数契约双闸
     * @return 本次失败是否应触发重播种（触发即闩锁，每漂移 episode 至多 true 一次）
     */
    public synchronized boolean onAuthFailure(FailureClass failureClass) {
        if (failureClass != FailureClass.CREDENTIAL) {
            // 可用性失败不是凭据证据：不计数也不清零（不清白证据面）
            return false;
        }
        if (!armed || reseedLatched) {
            return false;
        }
        consecutiveFailures++;
        if (consecutiveFailures >= RESEED_THRESHOLD) {
            reseedLatched = true;
            consecutiveFailures = 0;
            return true;
        }
        return false;
    }

    /** 成功登录复位：连续计数与闩锁同清（新一轮漂移重新起算）；武装态保持（单调）。 */
    public synchronized void onAuthSuccess() {
        consecutiveFailures = 0;
        reseedLatched = false;
    }
}
