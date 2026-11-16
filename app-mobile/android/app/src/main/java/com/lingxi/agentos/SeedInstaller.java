package com.lingxi.agentos;

import android.content.Context;
import android.content.SharedPreferences;
import android.util.Log;

import java.io.File;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.List;

/**
 * 种子安装器（2026-10-26 用户令「把解压安装变成安装的过程」）：解压+对账键落盘
 * 的单一实现，两个触发方共用——①app 打开时的 bootChain（首启/种子变更/清数据
 * 后）；②PreinstallService（覆盖安装完成广播，后台预装，不等用户打开）。
 *
 * 对账语义（2026-11-01 简化）：种子资产 SHA-256 digest 即对账键，digest 一致即
 * 跳过——venv 装配已在打包期预烤进种子（materialize_seed.py 烘焙
 * opt/agentos/plugin-venvs，start_stack.sh 经 AGENTOS_PLUGIN_VENVS_DIR 指向），
 * 设备端零装配，装配完成标记退役。升级装种子未变时零解压成本；类锁串行保证
 * bootChain 与后台预装不并发解包。
 */
public final class SeedInstaller {
    /** 与 KernelHostPlugin 同 tag：历史 logcat 过滤口径（-s LingxiKernelHost）不变。 */
    private static final String LOG_TAG = "LingxiKernelHost";

    private SeedInstaller() {
    }

    /**
     * 确保种子已解压（缺才做，幂等）。返回是否实际执行了解压。
     * 类锁串行：后台预装进行中时，bootChain 调用会等它完成再对账（此时多半
     * 直接命中跳过）。
     */
    public static boolean ensureInstalled(Context ctx, SharedPreferences prefs)
            throws IOException, InterruptedException {
        synchronized (SeedInstaller.class) {
            // 失败先落进度面再上抛：登录页/预装通知即时呈现具体原因
            try {
                return ensureInstalledLocked(ctx, prefs);
            } catch (Exception e) {
                BootProgress.setError(BootProgress.stage(), BootProgress.shortMessage(e));
                throw e;
            }
        }
    }

    private static boolean ensureInstalledLocked(Context ctx, SharedPreferences prefs)
            throws IOException, InterruptedException {
        {
            File rootfs = rootfsDir(ctx);
            File hostUserRoot = userRootDir(ctx);
            String digest = seedDigest(ctx);
            String seeded = prefs.getString(KernelHostPlugin.KEY_SEED_DIGEST, null);
            // digest 即对账（装配预烤后标记退役；.mobile_init_done 仅旧装机残留，
            // 随重解包清除半径清理，不再参与判定）
            boolean fresh = !digest.equals(seeded);
            if (!fresh) {
                Log.i(LOG_TAG, "seed up-to-date (digest 对账通过)，跳过解包");
                return false;
            }
            Log.i(LOG_TAG, "seed install start (seeded=" + seeded + " now=" + digest + ")");
            BootProgress.resetCounters();
            BootProgress.setStage(BootProgress.STAGE_EXTRACT);
            // 先迁出旧布局用户数据再重解包（R9-A：清除半径=种子部署面，用户数据豁免，
            // 迁移失败 fail-closed 不进解包——宁可起不来不可丢数据）
            SeedLayout.migrateLegacyUserRoot(
                    new File(rootfs, SeedLayout.LEGACY_USER_ROOT_REL),
                    new File(rootfs, SeedLayout.LEGACY_WORKSPACES_REL),
                    hostUserRoot);
            extractSeed(ctx, rootfs, hostUserRoot);
            prefs.edit().putString(KernelHostPlugin.KEY_SEED_DIGEST, digest).apply();
            Log.i(LOG_TAG, "seed install done");
            return true;
        }
    }

    /** 种子资产 SHA-256（流式，hex 小写；对账键真源）。 */
    private static String seedDigest(Context ctx) throws IOException {
        try (InputStream in = ctx.getAssets().open(KernelHostPlugin.SEED_ASSET)) {
            java.security.MessageDigest md;
            try {
                md = java.security.MessageDigest.getInstance("SHA-256");
            } catch (java.security.NoSuchAlgorithmException e) {
                throw new IOException("SHA-256 unavailable", e);
            }
            byte[] buf = new byte[1 << 20];
            int n;
            while ((n = in.read(buf)) > 0) {
                md.update(buf, 0, n);
            }
            StringBuilder hex = new StringBuilder(md.getDigestLength() * 2);
            for (byte b : md.digest()) {
                hex.append(Character.forDigit((b >> 4) & 0xf, 16));
                hex.append(Character.forDigit(b & 0xf, 16));
            }
            return hex.toString();
        }
    }

    private static void extractSeed(Context ctx, File rootfs, File hostUserRoot)
            throws IOException, InterruptedException {
        File seedCache = new File(ctx.getCacheDir(), KernelHostPlugin.SEED_ASSET);
        copyAsset(ctx, KernelHostPlugin.SEED_ASSET, seedCache);
        // 清除半径=种子部署面（SeedLayout：rootfs 整树+新旧装配残留面；用户数据
        // 目录显式豁免）。rootfs 内 bind 点随后建实——proot 对不存在的绑定路径会静默
        // 跳过，目录必须先行存在（宿主面由 prootCommand 统一建实）。
        SeedLayout.clearDeploymentFace(rootfs,
                new File(rootfs, SeedLayout.LEGACY_USER_ROOT_REL),
                new File(rootfs, SeedLayout.LEGACY_WORKSPACES_REL),
                hostUserRoot);
        if (!rootfs.mkdirs() && !rootfs.isDirectory()) {
            throw new IOException("rootfs dir create failed: " + rootfs);
        }
        ensureDir(new File(rootfs, SeedLayout.LEGACY_USER_ROOT_REL));
        ensureDir(new File(rootfs, SeedLayout.LEGACY_WORKSPACES_REL));
        String seedx = nativeLibPath(ctx, "libseedx.so");
        Log.i(LOG_TAG, "seedx unpack: " + seedCache.length() + " bytes -> " + rootfs);
        Process p = new ProcessBuilder(seedx,
                "--seed", seedCache.getAbsolutePath(),
                "--dest", rootfs.getAbsolutePath(),
                "--zst", "--progress-every", "20000")
                .redirectErrorStream(true).start();
        // 具体失败原因（条目+errno）只在 seedx stderr——尾行吸出随异常上屏，
        // 不依赖设备日志即可定位（「启动有任何问题都要显示出来」失败面契约）
        String[] seedxError = {null};
        pipeToLog(p.getInputStream(), "seedx", line -> {
            if (line.contains("[seedx][ERROR]")) {
                seedxError[0] = line;
            }
            long entries = BootProgress.parseSeedxEntries(line);
            if (entries >= 0) {
                BootProgress.setExtractEntries(entries);
            }
        });
        int code = p.waitFor();
        seedCache.delete();
        if (code != 0) {
            throw new IOException("seedx exit " + code + "（设备日志 tag=LingxiKernelHost）"
                    + seedxErrorSuffix(seedxError[0]));
        }
        if (!new File(rootfs, "opt/agentos/agentos-kernel").isFile()) {
            throw new IOException("seed unpacked but kernel binary missing");
        }
    }

    /** seedx [ERROR] 行剥前缀后随异常携带；无则空串（消息面只增不添噪）。 */
    static String seedxErrorSuffix(String errorLine) {
        if (errorLine == null) {
            return "";
        }
        String detail = errorLine.replaceFirst("^.*\\[seedx\\]\\[ERROR\\]\\s*", "");
        return detail.isEmpty() ? "" : "：" + detail;
    }

    /** proot 命令构造（种子内脚本与 start_stack 共一形态；startStack 亦复用）。 */
    public static List<String> prootCommand(Context ctx, File rootfs, SharedPreferences prefs,
                                            String guestScript) throws IOException {
        String lib = nativeLibDir(ctx);
        // 用户数据双 bind（R9-A）：guest 惯用路径不变，宿主落 rootfs 平级目录，
        // 种子重解包只清 rootfs 树。宿主面缺失即建实（proot 对不存在的绑定路径
        // 会静默跳过——数据落回 rootfs 内=下次升级连坐，故目录必须在此保证）。
        File hostUserRoot = userRootDir(ctx);
        File hostWorkspaces = new File(hostUserRoot, SeedLayout.WORKSPACES_DIR);
        ensureDir(hostUserRoot);
        ensureDir(hostWorkspaces);
        List<String> cmd = new ArrayList<>();
        cmd.add(lib + "/libproot.so");
        cmd.add("-0");
        cmd.add("-w");
        cmd.add("/opt/agentos");
        cmd.add("-r");
        cmd.add(rootfs.getAbsolutePath());
        cmd.add("-b");
        cmd.add(hostUserRoot.getAbsolutePath() + ":" + KernelHostPlugin.GUEST_USER_ROOT);
        cmd.add("-b");
        cmd.add(hostWorkspaces.getAbsolutePath() + ":" + KernelHostPlugin.GUEST_WORKSPACES);
        cmd.add("-b");
        cmd.add("/dev");
        cmd.add("-b");
        cmd.add("/proc");
        cmd.add("-b");
        cmd.add("/sys");
        cmd.add("/usr/bin/env");
        cmd.add("-i");
        cmd.add("HOME=/root");
        cmd.add("TERM=xterm");
        // proot loader 解包目录（PROOT_TMP_DIR）+ 通用临时目录，均指向 app 缓存
        cmd.add("TMPDIR=" + ctx.getCacheDir().getAbsolutePath());
        cmd.add("PROOT_TMP_DIR=" + ctx.getCacheDir().getAbsolutePath());
        cmd.add("PATH=/opt/agentos/python/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin");
        cmd.add("AGENTOS_KERNEL_PORT=" + KernelHostPlugin.KERNEL_PORT);
        // 空口令防御（ShellCredentialGuard）：任何时序下不得把空串传给内核——
        // 空口令触发内核自生成分支=口令漂移源，壳凭据自此恒败；补种后仍空
        // fail-closed 不起栈（宁可起不来不制造漂移）
        String adminPassword = new ShellCredentialGuard(
                () -> prefs.getString(KernelHostPlugin.KEY_ADMIN_PASSWORD, ""),
                () -> ensureCredentials(prefs)).requirePassword();
        if (adminPassword == null) {
            throw new IOException("admin password unavailable after ensureCredentials (fail-closed)");
        }
        cmd.addAll(KernelGuestEnv.credentialEntries(adminPassword,
                prefs.getString(KernelHostPlugin.KEY_TOKEN_SECRET, "")));
        cmd.add("/bin/bash");
        cmd.add(guestScript);
        return cmd;
    }

    /** 设备侧凭据补种（admin 口令/token secret 空则生成；env 即口令事实源前提）。 */
    public static void ensureCredentials(SharedPreferences prefs) {
        SharedPreferences.Editor edit = prefs.edit();
        if (isEmpty(prefs.getString(KernelHostPlugin.KEY_ADMIN_PASSWORD, null))) {
            edit.putString(KernelHostPlugin.KEY_ADMIN_PASSWORD, randomAlphaNum(16));
        }
        if (isEmpty(prefs.getString(KernelHostPlugin.KEY_TOKEN_SECRET, null))) {
            edit.putString(KernelHostPlugin.KEY_TOKEN_SECRET, randomAlphaNum(48));
        }
        edit.apply();
    }

    public static File rootfsDir(Context ctx) {
        return new File(ctx.getFilesDir(), "rootfs");
    }

    public static File userRootDir(Context ctx) {
        return new File(ctx.getFilesDir(), SeedLayout.USER_ROOT_DIR);
    }

    static String nativeLibDir(Context ctx) {
        return ctx.getApplicationInfo().nativeLibraryDir;
    }

    private static String nativeLibPath(Context ctx, String so) {
        return nativeLibDir(ctx) + "/" + so;
    }

    private static void copyAsset(Context ctx, String name, File dest) throws IOException {
        try (InputStream in = ctx.getAssets().open(name);
             java.io.OutputStream out = new java.io.FileOutputStream(dest)) {
            byte[] buf = new byte[1 << 20];
            int n;
            while ((n = in.read(buf)) > 0) {
                out.write(buf, 0, n);
            }
        }
    }

    private static void ensureDir(File dir) throws IOException {
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new IOException("dir create failed: " + dir);
        }
    }

    private static void pipeToLog(InputStream in, String tag,
                                  java.util.function.Consumer<String> onLine) {
        try (java.io.BufferedReader r = new java.io.BufferedReader(
                new java.io.InputStreamReader(in))) {
            String line;
            while ((line = r.readLine()) != null) {
                Log.i(LOG_TAG, "[" + tag + "] " + line);
                if (onLine != null) {
                    onLine.accept(line);
                }
            }
        } catch (IOException e) {
            Log.w(LOG_TAG, "[" + tag + "] pipe closed: " + e);
        }
    }

    private static boolean isEmpty(String s) {
        return s == null || s.isEmpty();
    }

    private static String randomAlphaNum(int len) {
        String alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789";
        java.security.SecureRandom random = new java.security.SecureRandom();
        StringBuilder sb = new StringBuilder(len);
        for (int i = 0; i < len; i++) {
            sb.append(alphabet.charAt(random.nextInt(alphabet.length())));
        }
        return sb.toString();
    }
}
