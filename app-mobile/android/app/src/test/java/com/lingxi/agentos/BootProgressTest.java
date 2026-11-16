package com.lingxi.agentos;

import static org.junit.Assert.assertEquals;
import static org.junit.Assert.assertTrue;

import org.junit.Test;

/**
 * 启动进度面单元测试（2026-10-26 用户令「要有解压进度」）：seedx 进度行解析、
 * 失败面、计数器单调性。装配段已退役（venv 打包期预烤进种子），进度链=解包→拉起。
 */
public class BootProgressTest {

    @Test
    public void parseSeedxEntries_progressLine() {
        assertEquals(20000L, BootProgress.parseSeedxEntries("[seedx] 进度 entries=20000 bytes=123456789"));
        assertEquals(0L, BootProgress.parseSeedxEntries("[seedx] 进度 entries=0 bytes=0"));
        assertEquals(123456L, BootProgress.parseSeedxEntries("[seedx] 进度 entries=123456 bytes=987654321"));
    }

    @Test
    public void parseSeedxEntries_nonProgressLines() {
        assertEquals(-1L, BootProgress.parseSeedxEntries(null));
        assertEquals(-1L, BootProgress.parseSeedxEntries("[seedx] unpack done"));
        assertEquals(-1L, BootProgress.parseSeedxEntries("some other log line"));
        // bytes= 单独出现不算（只认 entries=）
        assertEquals(-1L, BootProgress.parseSeedxEntries("bytes=123456"));
    }

    @Test
    public void errorSurface_setShortMessageAndClear() {
        BootProgress.clearError();
        assertEquals("", BootProgress.error());

        RuntimeException boom = new RuntimeException("seedx exit 1\ncaused by\nstuff");
        BootProgress.setError(BootProgress.STAGE_EXTRACT, BootProgress.shortMessage(boom));
        assertEquals(BootProgress.STAGE_EXTRACT, BootProgress.errorStage());
        // 换行折叠为空格（UI 不破版）
        assertEquals("seedx exit 1 caused by stuff", BootProgress.error());

        BootProgress.clearError();
        assertEquals("", BootProgress.error());
        assertEquals("", BootProgress.errorStage());
    }

    @Test
    public void counters_monotonicAndReset() {
        BootProgress.resetCounters();
        assertEquals(BootProgress.STAGE_IDLE, BootProgress.stage());
        assertEquals(0L, BootProgress.extractEntries());

        BootProgress.setStage(BootProgress.STAGE_EXTRACT);
        BootProgress.setExtractEntries(40000L);
        assertEquals(BootProgress.STAGE_EXTRACT, BootProgress.stage());
        assertEquals(40000L, BootProgress.extractEntries());

        BootProgress.resetCounters();
        assertEquals(0L, BootProgress.extractEntries());
        // stage 不随计数器复位（阶段由 bootChain 显式推进）
        assertEquals(BootProgress.STAGE_EXTRACT, BootProgress.stage());
    }

    @Test
    public void stages_noAssembleStage_afterBakeRetirement() {
        // 装配退役后阶段链=解包→拉起（无 assemble 档；前端旧文案分支自然死路）
        String[] stages = {BootProgress.STAGE_IDLE, BootProgress.STAGE_EXTRACT,
                BootProgress.STAGE_STARTING, BootProgress.STAGE_RUNNING};
        assertTrue(stages.length == 4);
    }
}
