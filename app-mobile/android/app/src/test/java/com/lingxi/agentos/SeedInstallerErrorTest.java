package com.lingxi.agentos;

import static org.junit.Assert.assertEquals;

import org.junit.Test;

/** seedx 失败原因上屏面：[ERROR] 尾行剥前缀随异常携带，无错行不加噪。 */
public class SeedInstallerErrorTest {

    @Test
    public void errorLine_stripsPrefix_keepsEntryAndErrno() {
        assertEquals("：条目解包失败 opt/agentos/agentos-kernel: No space left on device (os error 28)",
                SeedInstaller.seedxErrorSuffix(
                        "[seedx][ERROR] 条目解包失败 opt/agentos/agentos-kernel: No space left on device (os error 28)"));
    }

    @Test
    public void nullLine_emptySuffix() {
        assertEquals("", SeedInstaller.seedxErrorSuffix(null));
    }

    @Test
    public void bareMarker_emptySuffix_noNoise() {
        assertEquals("", SeedInstaller.seedxErrorSuffix("[seedx][ERROR]"));
    }
}
