package com.lingxi.agentos;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Handler;
import android.os.IBinder;
import android.os.Looper;
import android.util.Log;

/**
 * 覆盖安装后台预装（2026-10-26 用户令「把内核解压安装的过程变成安装的过程」）：
 * MY_PACKAGE_REPLACED 广播（PreinstallPackageReceiver）拉起，不打开 app 即开始
 * 种子解压，通知栏实时显示解压条目计数（与登录页 status() 同一 BootProgress
 * 进度面；venv 已打包期预烤进种子，预装链无装配段）。完成后通知移除；种子对账
 * 未变（同种子升级）秒退不扰。完成后用户首次打开 app，bootChain 对账命中跳过
 * 解压直接起栈=秒进。
 */
public class PreinstallService extends Service {
    private static final String TAG = KernelHostPlugin.LOG_TAG;
    private static final String CHANNEL_ID = "kernel_preinstall";
    private static final int NOTIFICATION_ID = 2001;

    private final Handler mainHandler = new Handler(Looper.getMainLooper());
    private final Object lastTextLock = new Object();
    private String lastText = "";
    private volatile boolean running;

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (running) {
            Log.i(TAG, "[preinstall] already running, skip");
            return START_NOT_STICKY;
        }
        running = true;
        NotificationManager nm = (NotificationManager) getSystemService(Context.NOTIFICATION_SERVICE);
        nm.createNotificationChannel(new NotificationChannel(CHANNEL_ID, "本机内核准备",
                NotificationManager.IMPORTANCE_LOW));
        startForeground(NOTIFICATION_ID, buildNotification("准备中…"));
        mainHandler.postDelayed(progressTick(nm), 1000);
        new Thread(() -> {
            try {
                SharedPreferences prefs = getSharedPreferences(
                        KernelHostPlugin.PREFS_NAME, Context.MODE_PRIVATE);
                boolean ran = SeedInstaller.ensureInstalled(getApplicationContext(), prefs);
                Log.i(TAG, "[preinstall] " + (ran ? "种子安装完成" : "种子对账通过，无需准备"));
            } catch (Exception e) {
                Log.e(TAG, "[preinstall] failed（下次打开 app 将重试）", e);
                BootProgress.setError(BootProgress.stage(), BootProgress.shortMessage(e));
            } finally {
                running = false;
                mainHandler.post(() -> {
                    stopForeground(true);
                    stopSelf();
                });
            }
        }, "kernel-preinstall").start();
        return START_NOT_STICKY;
    }

    /** 每秒轮询 BootProgress 刷新通知文案（与登录页 status() 同源）。 */
    private Runnable progressTick(NotificationManager nm) {
        return new Runnable() {
            @Override
            public void run() {
                if (!running) {
                    return;
                }
                String stage = BootProgress.stage();
                String text;
                if (BootProgress.STAGE_EXTRACT.equals(stage)) {
                    long n = BootProgress.extractEntries();
                    text = n > 0 ? "内核解压中（已解压 " + n + " 个文件）" : "内核解压中…";
                } else {
                    text = "本机内核准备中…";
                }
                boolean changed;
                synchronized (lastTextLock) {
                    changed = !text.equals(lastText);
                    lastText = text;
                }
                if (changed) {
                    nm.notify(NOTIFICATION_ID, buildNotification(text));
                }
                mainHandler.postDelayed(this, 1000);
            }
        };
    }

    private Notification buildNotification(String text) {
        return new Notification.Builder(this, CHANNEL_ID)
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentTitle("灵汐助手")
                .setContentText(text)
                .setOngoing(true)
                .build();
    }
}
