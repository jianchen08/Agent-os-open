package com.lingxi.agentos;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

/**
 * 覆盖安装完成广播（2026-10-26 用户令「把内核解压安装的过程变成安装的过程」）：
 * MY_PACKAGE_REPLACED 只在覆盖安装时发（首装无此广播，Android 安装器不执行
 * 应用代码是平台安全模型）——拉起 PreinstallService 后台预装种子，用户还没
 * 打开 app 解压装配就已经在进行，通知栏显示进度。种子对账未变时服务秒退。
 */
public class PreinstallPackageReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!Intent.ACTION_MY_PACKAGE_REPLACED.equals(intent.getAction())) {
            return;
        }
        Log.i(KernelHostPlugin.LOG_TAG, "[preinstall] package replaced → 启动后台种子预装");
        context.startService(new Intent(context, PreinstallService.class));
    }
}
