package com.lingxi.agentos;

import java.io.File;
import java.io.IOException;

/**
 * 种子升级用户数据保全面（R9-A）：种子重解包链的磁盘布局契约。
 *
 * 宿主侧三面分离：<files>/rootfs = 种子部署面（重解包全量替换）；
 * <files>/user_root = 用户数据根（会话库/.env/配置，proot bind → guest
 * /opt/agentos/user_root）；<files>/user_root/.ai_workspaces = 任务产物面
 * （proot bind → guest /opt/agentos/.ai_workspaces）。guest 侧惯用路径不变
 * （deploy 脚本与内核 env 口径零改动），宿主映射由 KernelHostPlugin.prootCommand
 * 单点承载——种子重解包只清 rootfs 树，用户数据天然在清除半径之外。
 *
 * 迁移契约（旧布局 → 新布局，一次性、幂等）：user_root 按「用户领地黑名单」
 * 迁——除装配残留面（plugin-venvs/.mobile_init_done，旧装机运行期装配的遗留物；
 * venv 登记处现打包期预烤于种子 opt/agentos/plugin-venvs，start_stack.sh 经
 * AGENTOS_PLUGIN_VENVS_DIR 指向，hostUserRoot 下 plugin-venvs 不再有生产者）
 * 外全部条目整体搬迁（rename 同卷原子，权限/mtime 保真）；.ai_workspaces 为纯
 * 用户数据整体搬迁。目标已存在 → 以新位为准（rename 原子性保证非全在即不在，
 * 目标在=已迁移或净装更新，旧位残件留待豁免面原地保全）。
 */
public final class SeedLayout {

    /** 宿主相对路径：用户数据根（rootfs 平级，位于 filesDir 下） */
    public static final String USER_ROOT_DIR = "user_root";
    /** 旧布局：user_root 在种子部署树内的相对路径（迁移源 / 防御纵深豁免点） */
    public static final String LEGACY_USER_ROOT_REL = "opt/agentos/user_root";
    /** 旧布局：任务产物面在种子部署树内的相对路径（迁移源 / 防御纵深豁免点） */
    public static final String LEGACY_WORKSPACES_REL = "opt/agentos/.ai_workspaces";
    /** 用户数据目录名（宿主新位与 guest 同名，bind 映射到 /opt/agentos/.ai_workspaces） */
    public static final String WORKSPACES_DIR = ".ai_workspaces";
    /** 装配残留面：旧装机 user_root 下运行期装配的 venv 登记处（仅升级清残留） */
    public static final String ASSEMBLED_VENVS = "plugin-venvs";
    /** 装配残留面：旧装机装配完成标记（mobile_init 已退役，仅清除兼容） */
    public static final String ASSEMBLED_MARKER = ".mobile_init_done";

    private SeedLayout() {
    }

    /**
     * 旧布局用户数据一次性迁移（幂等）。
     *
     * @param legacyUserRoot  旧布局用户根（rootfs 内），不存在即无事可迁
     * @param legacyWorkspace 旧布局任务产物面（rootfs 内），整体搬迁
     * @param hostUserRoot    新布局用户数据根（filesDir/user_root）
     * @throws IOException 目标目录创建或搬迁失败——fail-closed，调用方不得进入重解包
     */
    public static void migrateLegacyUserRoot(File legacyUserRoot, File legacyWorkspace,
                                             File hostUserRoot) throws IOException {
        ensureDir(hostUserRoot);
        if (legacyUserRoot.isDirectory()) {
            File[] entries = legacyUserRoot.listFiles();
            if (entries == null) {
                throw new IOException("user_root unreadable: " + legacyUserRoot);
            }
            for (File entry : entries) {
                if (ASSEMBLED_VENVS.equals(entry.getName())
                        || ASSEMBLED_MARKER.equals(entry.getName())) {
                    continue; // 装配残留面=部署面所有物，不迁移（升级随清除半径清残留）
                }
                moveTo(new File(hostUserRoot, entry.getName()), entry);
            }
        }
        if (legacyWorkspace.isDirectory()) {
            moveTo(new File(hostUserRoot, WORKSPACES_DIR), legacyWorkspace);
        }
    }

    /**
     * 种子部署面清除：rootfs 整树清除（legacy user_root 与 legacy .ai_workspaces
     * 显式豁免=防御纵深）+ 新旧两处装配残留面清除（hostUserRoot/plugin-venvs 为
     * 旧装机运行期装配遗留，与新种子预烤布局并行会陈旧且占空间——升级装即清）。
     */
    public static void clearDeploymentFace(File rootfs, File legacyUserRoot,
                                           File legacyWorkspace,
                                           File hostUserRoot) throws IOException {
        deleteTreePreserving(rootfs, legacyUserRoot, legacyWorkspace);
        deleteTree(new File(legacyUserRoot, ASSEMBLED_VENVS));
        deleteIfExists(new File(legacyUserRoot, ASSEMBLED_MARKER));
        deleteTree(new File(hostUserRoot, ASSEMBLED_VENVS));
        deleteIfExists(assembledMarker(hostUserRoot));
    }

    /** 装配完成标记（旧装机宿主位；装配退役后仅残留清除兼容读取）。 */
    public static File assembledMarker(File hostUserRoot) {
        return new File(hostUserRoot, ASSEMBLED_MARKER);
    }

    /** 单条目搬迁：目标已在=以新位为准（旧位原件保留在豁免面）；否则 rename 整体搬迁。 */
    private static void moveTo(File target, File source) throws IOException {
        if (target.exists()) {
            return;
        }
        if (!source.renameTo(target)) {
            throw new IOException("user data migrate failed: " + source + " -> " + target);
        }
    }

    private static void ensureDir(File dir) throws IOException {
        if (!dir.isDirectory() && !dir.mkdirs()) {
            throw new IOException("dir create failed: " + dir);
        }
    }

    /**
     * 整树删除，全部 preserve 子树原样豁免（自根向下逐层绕行——preserve 的祖先
     * 目录保留并深入，其余兄弟子树全清；单次遍历同时保全部豁免面）。
     */
    private static void deleteTreePreserving(File dir, File... preserves) throws IOException {
        File[] children = dir.listFiles();
        if (children == null) {
            return;
        }
        for (File c : children) {
            if (isSamePathOfAny(c, preserves)) {
                continue; // 豁免面本体：整子树原样跳过（子内容不清）
            }
            if (isAncestorOfAny(c, preserves)) {
                deleteTreePreserving(c, preserves); // 祖先：保留并深入绕行
                continue;
            }
            deleteTree(c);
        }
    }

    private static boolean isSamePathOfAny(File candidate, File[] targets) {
        for (File target : targets) {
            if (target.getAbsolutePath().equals(candidate.getAbsolutePath())) {
                return true;
            }
        }
        return false;
    }

    /** candidate 是任一 target 的真祖先。 */
    private static boolean isAncestorOfAny(File candidate, File[] targets) {
        for (File target : targets) {
            for (File p = target.getParentFile(); p != null; p = p.getParentFile()) {
                if (p.getAbsolutePath().equals(candidate.getAbsolutePath())) {
                    return true;
                }
            }
        }
        return false;
    }

    /**
     * 整树删除；文件删除失败即抛（fail-closed，半清部署面不得静默）。
     *
     * 两个设备实测语义必须显式处理（R9-A 修复轮 MuMu/Android 15 实证）：
     * ①符号链接一律按链接本体删除（NIO NOFOLLOW）——java.io.File.exists() 解引用，
     * 悬空链接（目标先被清掉）会被误判不存在而漏删，目录 rmdir 永远 ENOTEMPTY
     * （种子 rootfs 内 ubuntu base 携大量 .so 符号链接，此坑必踩）；
     * ②目录本体删除对 FUSE「子项刚清空、瞬时 ENOTEMPTY」容忍：短重试后仍失败
     * 且目录已空=视为清空达成；非空目录失败照抛。
     */
    private static void deleteTree(File f) throws IOException {
        java.nio.file.Path path = f.toPath();
        if (!java.nio.file.Files.exists(path, java.nio.file.LinkOption.NOFOLLOW_LINKS)) {
            return;
        }
        if (java.nio.file.Files.isSymbolicLink(path)) {
            java.nio.file.Files.delete(path); // 链接本体，不解引用
            return;
        }
        File[] children = f.listFiles();
        if (children != null) {
            for (File c : children) {
                deleteTree(c);
            }
        }
        if (f.delete()) {
            return;
        }
        if (f.isDirectory()) {
            for (int i = 0; i < 5; i++) {
                try {
                    Thread.sleep(100);
                } catch (InterruptedException e) {
                    Thread.currentThread().interrupt();
                    break;
                }
                if (f.delete()) {
                    return;
                }
            }
            File[] residue = f.listFiles();
            if (residue != null && residue.length == 0) {
                return; // 空残目录：清空达成，惰性残留无害
            }
        }
        throw new IOException("delete failed: " + f);
    }

    private static void deleteIfExists(File f) throws IOException {
        if (f.exists() && !f.delete()) {
            throw new IOException("delete failed: " + f);
        }
    }
}
