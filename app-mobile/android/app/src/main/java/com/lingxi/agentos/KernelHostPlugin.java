package com.lingxi.agentos;

import android.app.Activity;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.PowerManager;
import android.provider.Settings;
import android.util.Log;

import androidx.appcompat.app.AlertDialog;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.io.File;
import java.io.FileInputStream;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.SecureRandom;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

import io.capawesome.capacitorjs.plugins.foregroundservice.AndroidForegroundService;

/**
 * 移动端本机栈宿主（Phase 1 proot 产品化，R5）：APK 自足链路的唯一编排点。
 *
 * 链路（每次冷启动执行，幂等）：种子内容对账（资产 SHA-256 digest 键）→
 * （新种子才）旧布局用户数据一次性迁出（SeedLayout：files/user_root，rootfs
 * 平级）→ seedx 解包 assets/
 * rootfs_seed.tar.zst → files/rootfs（清除半径=种子部署面，用户数据目录豁免）→
 * proot 前台起核（deploy/start_stack.sh，内核+插件栈全在 proot 内=栈内
 * glibc cdylib 插件可用，P0.2 同构口径）→ 原生拉起前台服务持有进程树。
 * venv 装配后布局已打包期预烤进种子（opt/agentos/plugin-venvs，start_stack.sh
 * 经 AGENTOS_PLUGIN_VENVS_DIR 指向），设备端零装配（2026-11-01 设计公理，
 * mobile_init 退役）。用户数据面经 proot 双 bind 映射（files/user_root→
 * /opt/agentos/user_root、files/user_root/.ai_workspaces→
 * /opt/agentos/.ai_workspaces），guest 惯用路径不变，跨种子重解包天然不触碰
 * 用户数据（R9-A）。凭据（admin 口令/令牌种子）SecureRandom 生成后落
 * SharedPreferences，重启恢复同值——restart_recovery 会话连续性的前提。
 *
 * 后端模式分流（2026-10-15 用户需求）：remote 模式（用户设置指向远端后端）不拉起
 * 本地栈也不需要前台服务（FGS 为保活本地栈存在）——壳直连远端，运行消耗回落纯壳档；
 * 磁盘面（种子）保留，切回 local 即恢复离线可用。模式真值由前端在每次后端切换时
 * 经 setBackendMode 写入 SharedPreferences（与 server_base_url 同步变更），
 * 冷启动按存储值分流。运行中切换：remote 化=优雅停栈（内核 SIGTERM 走既有
 * graceful shutdown 面→proot 退→FGS 停）；local 化=重跑 bootChain（种子已解压
 * 则 warm 路径 ~13s）。
 *
 * 已知约束（如实记录）：targetSdk 36 下 Android 禁 app-data 目录 exec
 * （SELinux execute_no_trans）——rootfs 内二进制（env/bash/python/内核）的执行
 * 需要 root 环境（MuMu 验证口径 adb root + setenforce 0）；无 root 分发口径
 * （targetSdk 决策/jniLibs 全量装载）挂拍板，见 CI 车道台账 R5 轮。
 */
@CapacitorPlugin(name = "KernelHost")
public class KernelHostPlugin extends Plugin {

    public static final String LOG_TAG = "LingxiKernelHost";
    private static final String TAG = LOG_TAG;
    static final String PREFS_NAME = "lingxi_kernel";
    static final String KEY_ADMIN_PASSWORD = "admin_password";
    static final String KEY_TOKEN_SECRET = "token_secret";
    static final String KEY_SEED_DIGEST = "seed_digest";
    /** 后端模式（local=本机栈 | remote=纯壳直连远端），前端 setBackendMode 写入 */
    private static final String KEY_BACKEND_MODE = "backend_mode";
    /** 用户从通知手动停止后的 FGS 压抑制（栈重启即解除） */
    private static final String KEY_FGS_SUPPRESSED = "fgs_suppressed";
    static final String KERNEL_PORT = "9300";
    static final String SEED_ASSET = "rootfs_seed.tar.zst";
    /** 用户数据面 guest 惯用路径（deploy 脚本/内核 env 口径不变；宿主映射单点=本类 prootCommand） */
    static final String GUEST_USER_ROOT = "/opt/agentos/user_root";
    /** 任务产物面 guest 惯用路径（isolation workspace 基目录默认解析位，同上单点映射） */
    static final String GUEST_WORKSPACES = "/opt/agentos/.ai_workspaces";
    private static final String SERVICE_CHANNEL_ID = "lingxi_service";
    private static final int FGS_NOTIFICATION_ID = 1001;
    /** FGS 通知「停止」动作按钮 id（shell-push buttonClicked 监听同值） */
    static final int FGS_STOP_BUTTON_ID = 1002;
    /** 停栈等待期：内核 SIGTERM→sidecar drain→proot 退出的宽限上限 */
    private static final long STOP_STACK_WAIT_MS = 8000;
    /** /health 武装轮询节奏与上限（240×5s=20min 慢 boot 覆盖窗；超窗=不武装=不自愈，安全向） */
    private static final long HEALTH_ARMING_INTERVAL_MS = 5000;
    private static final int HEALTH_ARMING_MAX_ROUNDS = 240;

    private final ExecutorService executor = Executors.newSingleThreadExecutor();
    /** /health 武装轮询专用池（阻塞轮询不得占共用 executor——reseed/停栈同池会饿死） */
    private final ExecutorService healthArmingExecutor = Executors.newSingleThreadExecutor(r -> {
        Thread t = new Thread(r, "kernel-health-arming");
        t.setDaemon(true);
        return t;
    });
    private final java.util.concurrent.atomic.AtomicBoolean healthArmingRunning =
            new java.util.concurrent.atomic.AtomicBoolean(false);
    private final java.util.concurrent.atomic.AtomicBoolean booting =
            new java.util.concurrent.atomic.AtomicBoolean(false);
    private volatile Process stackProcess;

    /** 登录凭据失败看门狗（S5-A③ 漂移自愈计数面；reportLoginOutcome 消费） */
    private final ShellAuthWatchdog authWatchdog = new ShellAuthWatchdog();
    /** 栈进程树清理（重播种杀净面与既有停栈同一实现；进程面=壳内 sh pgrep/kill） */
    private final StackTreeStop stackTreeStop = new StackTreeStop(new StackTreeStop.ProcessProbe() {
        @Override
        public List<String> alive(String pgrepPattern) {
            return execShCapture("pgrep -f '" + pgrepPattern + "'");
        }

        @Override
        public void kill(String pid, boolean force) {
            execSh("kill " + (force ? "-9 " : "") + pid + " 2>/dev/null; true");
        }
    });

    /** 宿主 Activity 前后台态（onStart/onStop 维护）：FGS 生命周期分流的依据 */
    private static volatile boolean hostActivityForeground = false;
    /** 插件实例（Activity 生命周期静态入口用；插件随 bridge 存活，注册即赋值） */
    private static volatile KernelHostPlugin instance;

    @Override
    public void load() {
        instance = this;
        // 后端模式分流：remote 模式（纯壳直连远端）不编排本地栈也不拉前台服务，
        // 运行消耗回落纯壳档；模式真值=前端 setBackendMode 落 SharedPreferences。
        if (isRemoteMode()) {
            Log.i(TAG, "backend mode = remote: skip local stack boot (no kernel/proot/FGS)");
            return;
        }
        // 桥初始化即编排（后台线程）；失败只落日志与 prefs 状态，不崩壳——
        // REST 不可达在前端可见（连接失败），壳本体必须可用。
        // 单飞 + 一次重试：Activity 重建会重复 load()，双编排实例在
        // 解压/装配上竞态（R5 MuMu 实测坑㉝）；重试兜底半迁移状态收敛。
        runBootChainGuarded();
    }

    // ── Activity 前后台分流（FGS 通知 UX，2026-10-15 用户需求：前台活跃不显
    // 常驻通知，退后台/息屏才需要保活）──

    /** 回前台：停 FGS（用户在场，保活通知无价值；状态栏保持干净）。 */
    static void onHostActivityForeground() {
        hostActivityForeground = true;
        KernelHostPlugin p = instance;
        if (p != null) {
            p.stopForegroundService();
        }
    }

    /** 退后台/息屏：local 模式且本机栈在跑且用户未手动停止 → 起 FGS 保活。 */
    static void onHostActivityBackground() {
        hostActivityForeground = false;
        KernelHostPlugin p = instance;
        if (p != null) {
            p.maybeStartForegroundService();
        }
    }

    /** bootChain 单飞封装（load 与 setBackendMode('local') 共用，防双编排竞态坑㉝）。 */
    private void runBootChainGuarded() {
        executor.execute(() -> {
            if (!booting.compareAndSet(false, true)) {
                Log.i(TAG, "boot chain already running, skip");
                return;
            }
            try {
                bootChain();
            } catch (Exception first) {
                Log.e(TAG, "boot chain failed, retry once", first);
                try {
                    Thread.sleep(5000);
                    bootChain();
                } catch (Exception second) {
                    Log.e(TAG, "boot chain retry failed", second);
                }
            } finally {
                booting.set(false);
            }
        });
    }

    /**
     * 后端模式切换（前端保存连接地址时调用，与 localStorage server_base_url 同步
     * 变更）：remote 化=后台优雅停本地栈（内核既有 graceful shutdown 面→proot
     * 退→FGS 停）；local 化=后台重跑 bootChain（幂等，种子已解压走 warm 路径）。
     * 页面刷新与登录重试在前端（连接设置页保存即重载 + 本机档免填重试登录），
     * 原生侧只负责栈生命周期。
     */
    @PluginMethod
    public void setBackendMode(PluginCall call) {
        String mode = call.getString("mode");
        if (!"local".equals(mode) && !"remote".equals(mode)) {
            call.reject("mode must be local or remote");
            return;
        }
        SharedPreferences prefs = getContext()
                .getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
        prefs.edit().putString(KEY_BACKEND_MODE, mode).apply();
        JSObject data = new JSObject();
        data.put("mode", mode);
        call.resolve(data);
        final String target = mode;
        executor.execute(() -> {
            try {
                if ("remote".equals(target)) {
                    if (!isRemoteMode()) {
                        return; // 竞态防护：已切回 local 的后到任务不做停栈
                    }
                    stopStackChain();
                } else {
                    runBootChainGuarded();
                }
            } catch (Exception e) {
                Log.e(TAG, "backend mode transition failed: " + target, e);
            }
        });
    }

    private boolean isRemoteMode() {
        SharedPreferences prefs = getContext()
                .getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
        return "remote".equals(prefs.getString(KEY_BACKEND_MODE, "local"));
    }

    @PluginMethod
    public void status(PluginCall call) {
        JSObject data = new JSObject();
        Process p = stackProcess;
        data.put("running", p != null && p.isAlive());
        data.put("port", KERNEL_PORT);
        File rootfs = rootfsDir();
        data.put("seeded", new File(rootfs, "opt/agentos/agentos-kernel").isFile());
        // initialized=种子对账键在账（digest 落账=解包完成，venv 已随种子预烤）。
        // 旧语义=装配完成标记文件，装配退役后改由 digest 背书（键位契约不变）
        SharedPreferences prefs = getContext()
                .getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
        data.put("initialized", prefs.getString(KEY_SEED_DIGEST, null) != null);
        // 启动进度面（登录页连接前真实状态，BootProgress 契约只增不删）；
        // venv_done 键位保留恒 0（装配打包期预烤后设备端无装配段）
        data.put("stage", BootProgress.stage());
        data.put("extract_entries", BootProgress.extractEntries());
        data.put("venv_done", 0);
        data.put("boot_error", BootProgress.error());
        data.put("boot_error_stage", BootProgress.errorStage());
        call.resolve(data);
    }

    /**
     * 本机内核登录凭据（本机免填自动登录的凭据源）：admin 口令首启 SecureRandom
     * 生成后落应用私有 SharedPreferences（设备侧事实源，重启同值，不进打包面），
     * 此处仅提供给本 app 自己的 WebView——本机内核档零配置开箱即用的前提；
     * 自定义后端的账号体系属对方实例，前端不消费本方法（见 shellLocalAuth）。
     */
    @PluginMethod
    public void localCredentials(PluginCall call) {
        SharedPreferences prefs = getContext()
                .getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
        SeedInstaller.ensureCredentials(prefs);
        JSObject data = new JSObject();
        data.put("username", "admin");
        data.put("password", prefs.getString(KEY_ADMIN_PASSWORD, ""));
        call.resolve(data);
    }

    /**
     * 登录结果上报（S5-A③ 漂移自愈计数面；出桥契约只增不删）：前端对每次登录
     * 成功/失败上报本方法，仅本机内核档有意义。计数契约双闸在原生执法：
     * ①武装闸——内核 /health 首个 200（startHealthArming）之前看门狗不武装，
     * boot 期任何失败不累积（慢 boot 重试攒满阈值打杀健康内核树的假 reseed
     * 由此外科切除）；②类别闸——仅 kind="credential"（前端 axios 层唯一
     * HTTP 状态可见面按 classifyLoginFailure 判类后过桥；本内核凭据拒绝族=
     * 400 BAD_REQUEST「用户名或密码错误」+ 401/403）进连续计数，达阈值触发
     * 一次重播种自愈：全树杀净停栈 + 重跑 bootChain，内核按
     * AGENTOS_ADMIN_PASSWORD env 每靴重置口令（漂移与限速表一并清除）；
     * kind 缺失/非 credential 按可用性处理不计数（破坏面在误 reseed，缺证
     * 据时按不计是 fail-safe 方向）。成功登录复位看门狗（连续语义 + 闩锁
     * 复位，至多一轮/漂移 episode 防死循环）。
     * 远端模式不计数不触发（远端口令错与本地栈无关，重播种无意义）；
     * 429 限速锁由前端不上报（终态，不是凭据证据）。
     */
    @PluginMethod
    public void reportLoginOutcome(PluginCall call) {
        Boolean success = call.getBoolean("success");
        if (success == null) {
            call.reject("success (boolean) is required");
            return;
        }
        String kind = call.getString("kind");
        call.resolve();
        if (Boolean.TRUE.equals(success)) {
            authWatchdog.onAuthSuccess();
            return;
        }
        if (isRemoteMode()) {
            Log.i(TAG, "auth failure in remote mode: reseed watchdog not applicable");
            return;
        }
        ShellAuthWatchdog.FailureClass failureClass = "credential".equals(kind)
                ? ShellAuthWatchdog.FailureClass.CREDENTIAL
                : ShellAuthWatchdog.FailureClass.AVAILABILITY;
        executor.execute(() -> {
            if (!authWatchdog.onAuthFailure(failureClass)) {
                return;
            }
            Log.w(TAG, "consecutive auth failures >= " + ShellAuthWatchdog.RESEED_THRESHOLD
                    + ": reseeding local stack (kernel env password reset on boot)");
            try {
                // 全树杀净后再起（native 内核可幸存强停占 9300=自愈空转，真机实测）
                stopStackChain();
            } catch (Exception e) {
                Log.e(TAG, "reseed stop failed", e);
            }
            runBootChainGuarded();
        });
    }

    // ── 编排链 ──────────────────────────────────────────────────────────────

    private File rootfsDir() {
        return SeedInstaller.rootfsDir(getContext());
    }

    private void bootChain() {
        try {
            Context ctx = getContext();
            File rootfs = rootfsDir();
            SharedPreferences prefs = ctx.getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE);
            // 种子安装单点=SeedInstaller（覆盖安装后台预装 PreinstallService 共用；
            // 对账未变即零解压直接返回——升级装不重复解压 250MB）
            BootProgress.clearError();
            SeedInstaller.ensureInstalled(ctx, prefs);
            SeedInstaller.ensureCredentials(prefs);
            BootProgress.setStage(BootProgress.STAGE_STARTING);
            startStack(ctx, rootfs, prefs);
            BootProgress.setStage(BootProgress.STAGE_RUNNING);
            // /health 武装轮询（看门狗计数契约①武装闸）：首个 200 前不武装
            startHealthArming();
            // 栈真起来了：解除用户停止压抑制（停止后下次栈重启恢复保活语义）
            prefs.edit().putBoolean(KEY_FGS_SUPPRESSED, false).apply();
            // FGS 只在退后台时挂（前台活跃状态栏保持干净；回后台由
            // onHostActivityBackground 补挂）
            maybeStartForegroundService();
            maybeBatteryGuidance(ctx);
        } catch (Exception e) {
            Log.e(TAG, "boot chain failed", e);
            // 失败面透出（登录页即时呈现具体原因，不再傻等超时窗）
            BootProgress.setError(BootProgress.stage(), BootProgress.shortMessage(e));
        }
    }

    private void startStack(Context ctx, File rootfs, SharedPreferences prefs) throws IOException {
        // 幂等：栈已在跑则不动（冷启动双载/热重载场景）
        Process old = stackProcess;
        if (old != null && old.isAlive()) {
            Log.i(TAG, "stack already alive, skip");
            return;
        }
        File logs = new File(ctx.getFilesDir(), "logs");
        if (!logs.isDirectory() && !logs.mkdirs()) {
            Log.w(TAG, "logs dir create failed");
        }
        File log = new File(logs, "kernel.log");
        ProcessBuilder pb = new ProcessBuilder(
                SeedInstaller.prootCommand(ctx, rootfs, prefs, "/opt/agentos/deploy/start_stack.sh"));
        pb.environment().put("PROOT_TMP_DIR", ctx.getCacheDir().getAbsolutePath());
        pb.environment().put("TMPDIR", ctx.getCacheDir().getAbsolutePath());
        pb.environment().put("LD_LIBRARY_PATH", SeedInstaller.nativeLibDir(ctx));
        // 外挂 loader（Termux memfd 版，app 域安全）：proot 的 extract_loader 自解包
        // 路径在 untrusted_app_27 被 SELinux 拒 ioctl → 瞬死；PROOT_LOADER env 指向
        // 随 jniLibs 分发的 loader 后自解包不再触发（2026-11-01 真机实证）。
        // 仅 arm64 包分发该 loader；x86_64 走 proot 内置 extract_loader——对不存在
        // 的文件注入 PROOT_LOADER 会让 proot execve 直接 ENOENT（MuMu 2026-11-09 实证）。
        File prootLoader = new File(SeedInstaller.nativeLibDir(ctx), "libproot_loader.so");
        if (prootLoader.isFile()) {
            pb.environment().put("PROOT_LOADER", prootLoader.getAbsolutePath());
        }
        pb.redirectErrorStream(true);
        pb.redirectOutput(log);
        stackProcess = pb.start();
        Log.i(TAG, "stack started port=" + KERNEL_PORT);
    }

    /**
     * /health 武装轮询（看门狗计数契约①武装闸）：内核 /health 首个 200 之前
     * 看门狗整体不武装。内核 HTTP 绑定晚于插件注册与管理员播种（start_server
     * 在 G2 校验与 seed_admin_user 之后），绑定前 9300 恒连接拒绝，首个 200=
     * 内核全功能就绪的唯一事实——boot 期连接失败/超时自此不进计数面。5s 间隔
     * GET {@code http://127.0.0.1:<KERNEL_PORT>/health}，连接拒绝/超时/非 2xx
     * 一律下轮再来；20 分钟上限（超窗未通=不武装=不自愈，破坏面方向安全）。
     * 独立单线程跑，不占共用 executor（阻塞轮询不得饿死 reseed/停栈路径）；
     * 已在跑则跳过（单飞——轮询命中的就是 9300 上的当前栈，跨 boot 复用无
     * 歧义；armed 幂等，重复 200 无副作用）。
     */
    private void startHealthArming() {
        if (!healthArmingRunning.compareAndSet(false, true)) {
            return;
        }
        healthArmingExecutor.execute(() -> {
            try {
                String spec = "http://127.0.0.1:" + KERNEL_PORT + "/health";
                for (int round = 0; round < HEALTH_ARMING_MAX_ROUNDS; round++) {
                    if (authWatchdog.isArmed()) {
                        return;
                    }
                    if (httpOk(spec)) {
                        authWatchdog.arm();
                        Log.i(TAG, "kernel /health 200: auth watchdog armed");
                        return;
                    }
                    try {
                        Thread.sleep(HEALTH_ARMING_INTERVAL_MS);
                    } catch (InterruptedException ie) {
                        Thread.currentThread().interrupt();
                        return;
                    }
                }
                Log.w(TAG, "kernel /health unreachable within arming window, watchdog stays unarmed");
            } finally {
                healthArmingRunning.set(false);
            }
        });
    }

    /** GET 2xx 判定（连接拒绝/超时/非 2xx 一律 false）；2s 超时不占轮询节奏。 */
    private static boolean httpOk(String spec) {
        HttpURLConnection conn = null;
        try {
            conn = (HttpURLConnection) new URL(spec).openConnection();
            conn.setConnectTimeout(2000);
            conn.setReadTimeout(2000);
            return conn.getResponseCode() / 100 == 2;
        } catch (Exception e) {
            return false;
        } finally {
            if (conn != null) {
                conn.disconnect();
            }
        }
    }

    /** FGS 挂起条件（2026-10-15 通知 UX）：仅 local 模式 + 本机栈在跑 + app 已
     * 退后台 + 用户未从通知手动停止。前台活跃/remote 模式状态栏保持干净。 */
    private void maybeStartForegroundService() {
        if (isRemoteMode()) {
            return;
        }
        if (hostActivityForeground) {
            return;
        }
        if (getContext().getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE)
                .getBoolean(KEY_FGS_SUPPRESSED, false)) {
            Log.i(TAG, "FGS suppressed (user stopped local stack from notification)");
            return;
        }
        Process p = stackProcess;
        if (p == null || !p.isAlive()) {
            return;
        }
        startForegroundServiceNative(getContext());
    }

    /** 停 FGS（回前台即调；服务停则常驻通知随之移除）。 */
    private void stopForegroundService() {
        try {
            getContext().stopService(new Intent(getContext(), AndroidForegroundService.class));
        } catch (Exception e) {
            Log.w(TAG, "stop foreground service failed: " + e);
        }
    }

    /** 用户从通知「停止」按钮终停本地栈（stopLocalStack）：压抑制 FGS 直至下次
     * 栈重启（bootChain 成功即解除），停栈后回退纯壳直到用户再进本机档。 */
    @PluginMethod
    public void stopLocalStack(PluginCall call) {
        call.resolve();
        executor.execute(() -> {
            try {
                getContext().getSharedPreferences(PREFS_NAME, Activity.MODE_PRIVATE)
                        .edit().putBoolean(KEY_FGS_SUPPRESSED, true).apply();
                stopStackChain();
            } catch (Exception e) {
                Log.e(TAG, "user-stop local stack failed", e);
            }
        });
    }

    /** 原生拉起前台服务（Extras 契约对齐 @capawesome 插件 AndroidForegroundService）。
     * 渠道 LOW 重要性（状态栏无常驻图标，仅下拉抽屉「后台运行」分组可见）；
     * 通知文案中性 + 带停止动作（用户可手动终停本地栈）。 */
    private void startForegroundServiceNative(Context ctx) {
        android.app.NotificationManager nm = (android.app.NotificationManager)
                ctx.getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null && nm.getNotificationChannel(SERVICE_CHANNEL_ID) == null) {
            android.app.NotificationChannel channel = new android.app.NotificationChannel(
                    SERVICE_CHANNEL_ID, "后台运行", android.app.NotificationManager.IMPORTANCE_LOW);
            channel.setDescription("本地内核后台保活（可从通知停止）");
            nm.createNotificationChannel(channel);
        }
        int iconId = ctx.getResources().getIdentifier("ic_launcher_foreground", "drawable",
                ctx.getPackageName());
        android.os.Bundle nb = new android.os.Bundle();
        nb.putInt("id", FGS_NOTIFICATION_ID);
        nb.putInt("icon", iconId);
        nb.putString("title", "灵汐");
        nb.putString("body", "本地内核后台保活中，点「停止」可关闭");
        nb.putBoolean("silent", true);
        nb.putInt("serviceType", 0); // 0 = 用 manifest 声明的 dataSync 类型
        // buttons：停止动作（点击经插件 NotificationActionBroadcastReceiver →
        // JS buttonClicked 事件 → shell-push 调 KernelHost.stopLocalStack）。
        // buttons 必带（服务端 onStartCommand 无条件取键，缺键=NPE 杀 app，
        // MuMu 实测坑㉞）
        ArrayList<android.os.Bundle> buttons = new ArrayList<>();
        android.os.Bundle stopButton = new android.os.Bundle();
        stopButton.putInt("id", FGS_STOP_BUTTON_ID);
        stopButton.putString("title", "停止");
        buttons.add(stopButton);
        nb.putParcelableArrayList("buttons", buttons);
        Intent intent = new Intent(ctx, AndroidForegroundService.class);
        intent.putExtra("channelId", SERVICE_CHANNEL_ID);
        intent.putExtra("notification", nb);
        ctx.startForegroundService(intent);
        Log.i(TAG, "foreground service requested");
    }

    /** 电池优化白名单引导（未加白才弹；去设置=系统白名单页，免权限路由）。 */
    private void maybeBatteryGuidance(Context ctx) {
        PowerManager pm = (android.os.PowerManager) ctx.getSystemService(Context.POWER_SERVICE);
        if (pm != null && pm.isIgnoringBatteryOptimizations(ctx.getPackageName())) {
            return;
        }
        Activity activity = getActivity();
        if (activity == null || activity.isFinishing()) {
            return;
        }
        activity.runOnUiThread(() -> new AlertDialog.Builder(activity)
                .setTitle("允许后台运行")
                .setMessage("把灵汐助手加入电池优化白名单，可避免系统休眠时中断本机内核与长跑任务。")
                .setPositiveButton("去设置", (d, w) -> {
                    try {
                        Intent i = new Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS);
                        activity.startActivity(i);
                    } catch (Exception e) {
                        Log.w(TAG, "battery settings open failed: " + e);
                    }
                })
                .setNegativeButton("稍后", null)
                .show());
    }

    // ── 停栈（remote 化切换；内核既有 shutdown 面 = SIGTERM 触发
    // server.rs with_graceful_shutdown：sidecar drain + 逐 kill，防孤儿）──

    private void stopStackChain() {
        Log.i(TAG, "stopping local stack (graceful: kernel SIGTERM -> sidecar drain -> proot exit)");
        // 1. 只 TERM 内核（graceful 窗口内 proot 须存活转发：提前 TERM proot 会
        // 截断内核 sidecar drain；进程同属本 app UID，kill 权限自足）
        stackTreeStop.term(StackTreeStop.PATTERN_KERNEL);
        // 2. 等 proot 栈整体退出，超时强杀兜底
        Process p = stackProcess;
        if (p != null) {
            long deadline = android.os.SystemClock.elapsedRealtime() + STOP_STACK_WAIT_MS;
            while (p.isAlive() && android.os.SystemClock.elapsedRealtime() < deadline) {
                try {
                    Thread.sleep(300);
                } catch (InterruptedException ie) {
                    Thread.currentThread().interrupt();
                    break;
                }
            }
            if (p.isAlive()) {
                Log.w(TAG, "stack still alive after grace period, destroying");
                p.destroy();
            }
            stackProcess = null;
        }
        // 3. 宽限后残余 TERM（内核幸存+proot/python 异常路径存活；app 自身进程名
        // 不含 /opt/agentos 不自伤）
        stackTreeStop.termAll();
        // 4. 幸存者 KILL -9 升级（native 内核可幸存 TERM——真机实测：老内核占
        // 9300 持自生成口令，新栈起不来=重播种自愈空转）。杀不净如实告警，
        // 不静默假成功。
        if (!stackTreeStop.escalateForce()) {
            Log.w(TAG, "stack tree survivors persist after SIGKILL (port " + KERNEL_PORT
                    + " may stay occupied)");
        }
        // 5. FGS 停止（FGS 为保活本地栈存在；远端模式无栈可保）——通知随服务移除
        getContext().stopService(new Intent(getContext(), AndroidForegroundService.class));
        Log.i(TAG, "local stack stopped");
    }

    /** 壳内 sh 单行执行（栈进程面清理用）；失败只落日志（停栈兜底链自有后续步）。 */
    private void execSh(String script) {
        try {
            Process p = new ProcessBuilder("/system/bin/sh", "-c", script).start();
            if (!p.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
                p.destroy();
                Log.w(TAG, "exec sh timeout: " + script);
            }
        } catch (Exception e) {
            Log.w(TAG, "exec sh failed: " + e);
        }
    }

    /** 壳内 sh 单行执行并取 stdout 按行收集（pgrep pid 面用）；失败/无匹配=空表。 */
    private List<String> execShCapture(String script) {
        List<String> lines = new ArrayList<>();
        try {
            Process p = new ProcessBuilder("/system/bin/sh", "-c", script).start();
            try (java.io.BufferedReader r = new java.io.BufferedReader(
                    new java.io.InputStreamReader(p.getInputStream()))) {
                String line;
                while ((line = r.readLine()) != null) {
                    if (!line.trim().isEmpty()) {
                        lines.add(line.trim());
                    }
                }
            }
            if (!p.waitFor(5, java.util.concurrent.TimeUnit.SECONDS)) {
                p.destroy();
                Log.w(TAG, "exec sh capture timeout: " + script);
            }
        } catch (Exception e) {
            Log.w(TAG, "exec sh capture failed: " + e);
        }
        return lines;
    }

    // ── 工具 ────────────────────────────────────────────────────────────────

    private String nativeLibDir() {
        return getContext().getApplicationInfo().nativeLibraryDir;
    }

    private String nativeLibPath(String so) {
        return nativeLibDir() + "/" + so;
    }

    private static void copyAsset(Context ctx, String name, File dest) throws IOException {
        try (InputStream in = ctx.getAssets().open(name);
             OutputStream out = new FileOutputStream(dest)) {
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

}
