package com.lingxi.agentos;

/**
 * 本机启动进度面（进程级单例，用户令 2026-10-26「连接时要检测内核状态/要有
 * 解压进度」「安装过程要有进度」）：写入方=bootChain（app 内）与 PreinstallService
 * （覆盖安装后台预装），读取方=KernelHost.status() 桥与预装通知。volatile=
 * 写读跨线程；进程内同一时刻只允许一条解压流水（SeedInstaller 类锁串行）。
 *
 * 解压条目数来自 seedx --progress-every 的进度行（stderr 合流 stdout 进管道），
 * 无先验总数——进度以单调计数呈现，不伪装百分比。装配段已退役（venv 打包期
 * 预烤进种子，设计公理 2026-11-01），进度链=解包→拉起。
 */
public final class BootProgress {
    public static final String STAGE_IDLE = "idle";
    public static final String STAGE_EXTRACT = "extract";
    public static final String STAGE_STARTING = "starting";
    public static final String STAGE_RUNNING = "running";

    private static volatile String stage = STAGE_IDLE;
    private static volatile long extractEntries = 0;
    /** 启动失败面（空串=无错）：bootChain/预装任一阶段失败即记，status() 透出
     *  给登录页即时呈现——「启动有任何问题都要记得显示出来」（用户令 2026-10-26） */
    private static volatile String error = "";
    private static volatile String errorStage = "";

    private BootProgress() {
    }

    public static void setStage(String value) {
        stage = value;
    }

    public static String stage() {
        return stage;
    }

    public static void setExtractEntries(long value) {
        extractEntries = value;
    }

    public static long extractEntries() {
        return extractEntries;
    }

    public static void resetCounters() {
        extractEntries = 0;
    }

    /** 记录启动失败（阶段=失败发生时所处阶段；message 截断到 300 字符）。 */
    public static void setError(String failedStage, String message) {
        errorStage = failedStage == null ? "" : failedStage;
        String m = message == null ? "" : message;
        error = m.length() > 300 ? m.substring(0, 300) : m;
    }

    public static void clearError() {
        error = "";
        errorStage = "";
    }

    public static String error() {
        return error;
    }

    public static String errorStage() {
        return errorStage;
    }

    /** 异常 → 单行短消息（message 优先，缺省类名；换行折叠防 UI 破版）。 */
    public static String shortMessage(Throwable e) {
        String m = e.getMessage();
        if (m == null || m.isEmpty()) {
            m = e.getClass().getSimpleName();
        }
        return m.replaceAll("[\\r\\n]+", " ");
    }

    /** seedx 进度行 → 累计解包条目数（形态「[seedx] 进度 entries=N bytes=M」）；非进度行返回 -1。 */
    public static long parseSeedxEntries(String line) {
        if (line == null) {
            return -1;
        }
        java.util.regex.Matcher m = java.util.regex.Pattern
                .compile("entries=(\\d+)").matcher(line);
        return m.find() ? Long.parseLong(m.group(1)) : -1;
    }
}
