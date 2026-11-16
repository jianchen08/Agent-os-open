package com.lingxi.agentos;

import static org.junit.Assert.assertArrayEquals;
import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.util.Arrays;

import org.junit.Before;
import org.junit.Rule;
import org.junit.Test;
import org.junit.rules.TemporaryFolder;

/**
 * 种子升级用户数据保全契约测试（R9-A，本地 JVM 单测，纯 java.io 零 Android 依赖）。
 *
 * 复现面 = KernelHostPlugin 升级重解包链的文件层语义：旧布局（用户数据同栖
 * rootfs）→ 跨种子升级（迁移 → 部署面清除 → 模拟新种子解包）→ 数据保全。
 * 三条任务书主测：①旧布局造数据（会话+key+产物）升级后保全；②旧布局迁移到
 * 新布局且数据完好；③纯部署面正常替换。另锁：防御纵深豁免 / 幂等 / 目标已存
 * 在以新位为准。
 */
public class SeedLayoutTest {

    @Rule
    public TemporaryFolder tmp = new TemporaryFolder();

    private File rootfs;       // <files>/rootfs 种子部署树
    private File hostUserRoot; // <files>/user_root 用户数据根（新布局）
    private File legacyUserRoot;
    private File legacyWorkspace;

    private static final byte[] DB_BYTES =
            "sqlite-page-0-会话库字节样".getBytes(StandardCharsets.UTF_8);
    private static final String ENV_TEXT = "MINIMAX_API_KEY=sk-masked-1\nOTHER_API_KEY=sk-masked-2\n";
    private static final byte[] ARTIFACT_BYTES =
            "R9-PRE-UPGRADE-MARKER".getBytes(StandardCharsets.UTF_8);

    @Before
    public void setUp() throws IOException {
        rootfs = tmp.newFolder("files");
        rootfs = new File(rootfs, "rootfs");
        assertTrue(rootfs.mkdirs());
        hostUserRoot = new File(rootfs.getParentFile(), SeedLayout.USER_ROOT_DIR);
        legacyUserRoot = new File(rootfs, SeedLayout.LEGACY_USER_ROOT_REL);
        legacyWorkspace = new File(rootfs, SeedLayout.LEGACY_WORKSPACES_REL);
    }

    /** 旧布局造数据：会话库 + key .env + 任务产物 + 未知用户条目 + 装配面 + 部署面。 */
    private void plantLegacyLayout() throws IOException {
        writeFile(new File(legacyUserRoot, "agentos_kernel.db"), DB_BYTES);
        writeFile(new File(legacyUserRoot, ".env"), ENV_TEXT.getBytes(StandardCharsets.UTF_8));
        writeFile(new File(legacyUserRoot, "userdata/custom.bin"), new byte[]{1, 2, 3});
        writeFile(new File(legacyWorkspace, "tasks/t1/artifact.txt"), ARTIFACT_BYTES);
        writeFile(new File(legacyWorkspace, "sessions/abc123/state.json"), "{}".getBytes(StandardCharsets.UTF_8));
        writeFile(new File(legacyUserRoot, SeedLayout.ASSEMBLED_VENVS + "/llm_core/bin/python"), new byte[]{0x7f});
        writeFile(new File(legacyUserRoot, SeedLayout.ASSEMBLED_MARKER),
                "assembled=old".getBytes(StandardCharsets.UTF_8));
        writeFile(new File(rootfs, "opt/agentos/agentos-kernel"), "OLD-KERNEL".getBytes(StandardCharsets.UTF_8));
    }

    /** 升级序列（与 bootChain fresh 分支同序）：迁移 → 部署面清除 → 模拟新种子解包。 */
    private void runUpgradeSequence() throws IOException {
        SeedLayout.migrateLegacyUserRoot(legacyUserRoot, legacyWorkspace, hostUserRoot);
        SeedLayout.clearDeploymentFace(rootfs, legacyUserRoot, legacyWorkspace, hostUserRoot);
        // 模拟 seedx 新种子解包（部署面全量换新）
        writeFile(new File(rootfs, "opt/agentos/agentos-kernel"), "NEW-KERNEL".getBytes(StandardCharsets.UTF_8));
        writeFile(new File(rootfs, "opt/agentos/deploy/start_stack.sh"),
                "new".getBytes(StandardCharsets.UTF_8));
    }

    @Test
    public void upgradeAcrossSeeds_preservesUserData() throws IOException {
        plantLegacyLayout();
        long dbMtime = new File(legacyUserRoot, "agentos_kernel.db").lastModified();
        runUpgradeSequence();
        // 会话库 / key / 任务产物 / 未知用户条目：内容字节级保全
        assertArrayEquals(DB_BYTES, readBytes(new File(hostUserRoot, "agentos_kernel.db")));
        assertEquals(ENV_TEXT, readText(new File(hostUserRoot, ".env")));
        assertArrayEquals(ARTIFACT_BYTES,
                readBytes(new File(hostUserRoot, SeedLayout.WORKSPACES_DIR + "/tasks/t1/artifact.txt")));
        assertEquals("{}", readText(new File(hostUserRoot,
                SeedLayout.WORKSPACES_DIR + "/sessions/abc123/state.json")));
        assertArrayEquals(new byte[]{1, 2, 3},
                readBytes(new File(hostUserRoot, "userdata/custom.bin")));
        // rename 同卷搬迁：mtime 语义保真
        assertEquals(dbMtime, new File(hostUserRoot, "agentos_kernel.db").lastModified());
        // 新种子部署面在位（替换成立）
        assertEquals("NEW-KERNEL", readText(new File(rootfs, "opt/agentos/agentos-kernel")));
    }

    @Test
    public void legacyLayout_isMigratedToSiblingUserRoot() throws IOException {
        plantLegacyLayout();
        runUpgradeSequence();
        // 旧位置不再持有用户数据（搬迁走，非复制残留）
        assertFalse(new File(legacyUserRoot, "agentos_kernel.db").exists());
        assertFalse(new File(legacyUserRoot, ".env").exists());
        assertFalse(new File(legacyUserRoot, "userdata").exists());
        assertFalse(legacyWorkspace.exists());
        // 新位置装配残留面已清（旧装机运行期装配遗留，升级装即清，不得幸存）
        assertFalse(new File(hostUserRoot, SeedLayout.ASSEMBLED_VENVS).exists());
        assertFalse(SeedLayout.assembledMarker(hostUserRoot).exists());
    }

    @Test
    public void pureDeploymentFace_isReplacedNormally() throws IOException {
        // 无用户数据的净部署树 + 上一次装配残留（新布局稳态跨种子升级）
        writeFile(new File(rootfs, "opt/agentos/agentos-kernel"), "OLD".getBytes(StandardCharsets.UTF_8));
        writeFile(new File(rootfs, "opt/agentos/plugins/shared/x/plugin.py"),
                "old".getBytes(StandardCharsets.UTF_8));
        writeFile(new File(hostUserRoot, SeedLayout.ASSEMBLED_VENVS + "/t/bin/python"), new byte[]{1});
        writeFile(SeedLayout.assembledMarker(hostUserRoot), "assembled=prev".getBytes(StandardCharsets.UTF_8));
        SeedLayout.migrateLegacyUserRoot(legacyUserRoot, legacyWorkspace, hostUserRoot);
        SeedLayout.clearDeploymentFace(rootfs, legacyUserRoot, legacyWorkspace, hostUserRoot);
        // 部署面与装配面全清（解包器该全量覆盖的路径无幸存）
        assertFalse(new File(rootfs, "opt/agentos/agentos-kernel").exists());
        assertFalse(new File(rootfs, "opt/agentos/plugins").exists());
        assertFalse(new File(hostUserRoot, SeedLayout.ASSEMBLED_VENVS).exists());
        assertFalse(SeedLayout.assembledMarker(hostUserRoot).exists());
        // 模拟解包后新种子在位
        writeFile(new File(rootfs, "opt/agentos/agentos-kernel"), "NEW".getBytes(StandardCharsets.UTF_8));
        assertEquals("NEW", readText(new File(rootfs, "opt/agentos/agentos-kernel")));
    }

    @Test
    public void clearDeploymentFace_sparesUserDataLeftInLegacyDirs() throws IOException {
        // 防御纵深锁定：即便迁移被跳过，清除半径也不得吞用户数据（R9-A 回归锁）
        plantLegacyLayout();
        SeedLayout.clearDeploymentFace(rootfs, legacyUserRoot, legacyWorkspace, hostUserRoot);
        assertArrayEquals(DB_BYTES, readBytes(new File(legacyUserRoot, "agentos_kernel.db")));
        assertEquals(ENV_TEXT, readText(new File(legacyUserRoot, ".env")));
        assertArrayEquals(ARTIFACT_BYTES,
                readBytes(new File(legacyWorkspace, "tasks/t1/artifact.txt")));
        // 装配面与种子部署面照清
        assertFalse(new File(legacyUserRoot, SeedLayout.ASSEMBLED_VENVS).exists());
        assertFalse(new File(legacyUserRoot, SeedLayout.ASSEMBLED_MARKER).exists());
        assertFalse(new File(rootfs, "opt/agentos/agentos-kernel").exists());
    }

    @Test
    public void migrate_isIdempotent_noDuplicateResidue() throws IOException {
        plantLegacyLayout();
        SeedLayout.migrateLegacyUserRoot(legacyUserRoot, legacyWorkspace, hostUserRoot);
        SeedLayout.migrateLegacyUserRoot(legacyUserRoot, legacyWorkspace, hostUserRoot);
        assertArrayEquals(DB_BYTES, readBytes(new File(hostUserRoot, "agentos_kernel.db")));
        // 旧位置只剩装配面两件（用户条目零残留、无重复搬迁）
        String[] remaining = legacyUserRoot.list();
        Arrays.sort(remaining);
        assertArrayEquals(new String[]{SeedLayout.ASSEMBLED_MARKER, SeedLayout.ASSEMBLED_VENVS}, remaining);
        assertFalse(legacyWorkspace.exists());
    }

    @Test
    public void migrate_targetExists_keepsNewLocationCopy() throws IOException {
        plantLegacyLayout();
        String newer = "NEWER_KEY=1\n";
        writeFile(new File(hostUserRoot, ".env"), newer.getBytes(StandardCharsets.UTF_8));
        SeedLayout.migrateLegacyUserRoot(legacyUserRoot, legacyWorkspace, hostUserRoot);
        // 目标已存在 → 以新位为准；旧位原件不删不覆盖（豁免面原地保全）
        assertEquals(newer, readText(new File(hostUserRoot, ".env")));
        assertEquals(ENV_TEXT, readText(new File(legacyUserRoot, ".env")));
    }

    @Test
    public void clearDeploymentFace_danglingSymlinksDoNotBlockWipe() throws IOException {
        // 设备实测语义锁（MuMu/Android 15）：悬空符号链接必须按链接本体删除——
        // File.exists() 解引用会把「目标先被清掉的链接」误判不存在而漏删，
        // 目录 rmdir 永远 ENOTEMPTY（种子 rootfs 内 ubuntu base .so 链接必踩）。
        org.junit.Assume.assumeTrue("环境无符号链接权限，跳过", makeSymlinkIsSupported());
        File real = new File(rootfs, "usr/lib/x86_64-linux-gnu");
        writeFile(new File(real, "libreal.so"), new byte[]{1});
        java.nio.file.Files.createSymbolicLink(
                new File(real, "libdangling.so.0").toPath(),
                java.nio.file.Paths.get("libgone.so.0.0"));
        File linkDir = new File(rootfs, "usr/lib/aliased");
        writeFile(new File(rootfs, "usr/lib/realdir/f.txt"), new byte[]{2});
        java.nio.file.Files.createSymbolicLink(linkDir.toPath(),
                java.nio.file.Paths.get("realdir"));
        SeedLayout.clearDeploymentFace(rootfs, legacyUserRoot, legacyWorkspace, hostUserRoot);
        assertFalse(new File(rootfs, "usr/lib/x86_64-linux-gnu").exists());
        assertFalse(new File(rootfs, "usr/lib/aliased").exists());
        // 豁免面不受影响
        assertTrue(legacyUserRoot.isDirectory() || !legacyUserRoot.exists());
    }

    private static boolean makeSymlinkIsSupported() {
        try {
            File probe = java.nio.file.Files.createTempFile("symlinkprobe", ".tmp").toFile();
            java.nio.file.Files.delete(probe.toPath());
            java.nio.file.Files.createSymbolicLink(
                    new File(probe.getParentFile(), "symlinkprobe-link").toPath(), probe.toPath());
            new File(probe.getParentFile(), "symlinkprobe-link").delete();
            return true;
        } catch (IOException | UnsupportedOperationException | SecurityException e) {
            return false;
        }
    }

    private static void writeFile(File f, byte[] bytes) throws IOException {
        File parent = f.getParentFile();
        if (!parent.isDirectory() && !parent.mkdirs()) {
            throw new IOException("mkdirs failed: " + parent);
        }
        try (FileOutputStream out = new FileOutputStream(f)) {
            out.write(bytes);
        }
    }

    private static byte[] readBytes(File f) throws IOException {
        return Files.readAllBytes(f.toPath());
    }

    private static String readText(File f) throws IOException {
        return new String(readBytes(f), StandardCharsets.UTF_8);
    }
}
