package com.lingxi.agentos;

import static org.junit.Assert.assertFalse;
import static org.junit.Assert.assertTrue;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

import org.junit.Test;

/**
 * 本机栈进程树清理契约测试（S5-A③ 重播种杀净面，本地 JVM 单测，纯 Java 零
 * Android 依赖；进程存在性经 ProcessProbe 抽象注入假存活集）。
 *
 * 真机实测教训锁定：native 内核可幸存 TERM——强停后老内核占 9300 持自生成
 * 口令，新栈起不来=重播种自愈空转。契约：TERM 全树（保内核 graceful
 * shutdown）→ 幸存者 KILL -9 升级 → 全灭与否如实回报（不静默假成功）。
 */
public class StackTreeStopTest {

    /** 假存活集探针：TERM/KILL 的响应性按进程可配（termProof=幸存 TERM；
     * killProof=连 KILL -9 都不死，用于杀不净回报面） */
    private static class FakeProbe implements StackTreeStop.ProcessProbe {
        final Map<String, Set<String>> alive = new LinkedHashMap<>();
        final List<String> killedTerm = new ArrayList<>();
        final List<String> killedForce = new ArrayList<>();
        final List<String> queriedPatterns = new ArrayList<>();
        final Set<String> termProof = new LinkedHashSet<>();
        final Set<String> killProof = new LinkedHashSet<>();

        FakeProbe(String kernelPids, String stackPids) {
            alive.put(StackTreeStop.PATTERN_KERNEL, set(kernelPids));
            alive.put(StackTreeStop.PATTERN_STACK_TREE, set(stackPids));
        }

        private static Set<String> set(String pids) {
            Set<String> out = new LinkedHashSet<>();
            if (pids != null && !pids.isEmpty()) {
                for (String pid : pids.split(",")) {
                    out.add(pid.trim());
                }
            }
            return out;
        }

        private Set<String> union() {
            Set<String> all = new LinkedHashSet<>();
            for (Set<String> s : alive.values()) {
                all.addAll(s);
            }
            return all;
        }

        @Override
        public List<String> alive(String pgrepPattern) {
            queriedPatterns.add(pgrepPattern);
            return new ArrayList<>(alive.getOrDefault(pgrepPattern, new LinkedHashSet<>()));
        }

        @Override
        public void kill(String pid, boolean force) {
            (force ? killedForce : killedTerm).add(pid);
            boolean survives = force ? killProof.contains(pid)
                    : termProof.contains(pid) || killProof.contains(pid);
            if (!survives) {
                alive.get(StackTreeStop.PATTERN_KERNEL).remove(pid);
                alive.get(StackTreeStop.PATTERN_STACK_TREE).remove(pid);
            }
        }
    }

    @Test
    public void termAll_signalsBothPatterns_notForce() {
        // 阶段一契约：内核+栈树双模式全扫，只 TERM（保 graceful shutdown=sidecar drain）
        FakeProbe probe = new FakeProbe("100", "200,201");
        Set<String> expected = probe.union();
        StackTreeStop stop = new StackTreeStop(probe);
        stop.termAll();
        assertTrue("全树每个 pid 恰收一次 TERM：" + probe.killedTerm,
                probe.killedTerm.size() == expected.size() && probe.killedTerm.containsAll(expected));
        assertTrue("阶段一不得跳级强杀", probe.killedForce.isEmpty());
        assertTrue(probe.queriedPatterns.contains(StackTreeStop.PATTERN_KERNEL));
        assertTrue(probe.queriedPatterns.contains(StackTreeStop.PATTERN_STACK_TREE));
    }

    @Test
    public void termSurvivor_escalatedToSigkill_andReportedClean() {
        // 主断言（重播种杀净）：TERM 幸存者升级 KILL -9，全灭如实回报 true
        FakeProbe probe = new FakeProbe("100", "200");
        probe.termProof.add("100"); // native 内核幸存 TERM 的真机形态
        StackTreeStop stop = new StackTreeStop(probe);
        stop.termAll();
        assertTrue("TERM 幸存者必须升级强杀", stop.escalateForce());
        assertTrue(probe.killedForce.contains("100"));
    }

    @Test
    public void forceProofSurvivor_reportedUnclean_notFalseSuccess() {
        // 杀不净（连 KILL -9 都无效）必须回报 false：调用方据此告警，不假成功起栈
        FakeProbe probe = new FakeProbe("100", "");
        probe.killProof.add("100");
        StackTreeStop stop = new StackTreeStop(probe);
        stop.termAll();
        assertFalse(stop.escalateForce());
    }

    @Test
    public void emptyTree_escalateForce_reportsClean() {
        FakeProbe probe = new FakeProbe(null, null);
        StackTreeStop stop = new StackTreeStop(probe);
        assertTrue(stop.escalateForce());
    }

    @Test
    public void termKernelOnly_leavesStackTreeUntouched() {
        // 停栈第一步契约：只 TERM 内核——graceful 窗口内 proot 须存活转发，
        // 提前 TERM proot 会截断内核 sidecar drain
        FakeProbe probe = new FakeProbe("100", "200");
        StackTreeStop stop = new StackTreeStop(probe);
        stop.term(StackTreeStop.PATTERN_KERNEL);
        assertTrue(probe.killedTerm.contains("100"));
        assertFalse(probe.killedTerm.contains("200"));
        assertTrue(probe.killedForce.isEmpty());
    }

    @Test
    public void pidMatchedByBothPatterns_signaledOnce() {
        // 同一 pid 双模式命中去重：单发一次 TERM（LinkedHashSet 并集）
        FakeProbe probe = new FakeProbe("100", "100");
        StackTreeStop stop = new StackTreeStop(probe);
        stop.termAll();
        assertEqualsOnce(probe.killedTerm, "100");
    }

    private static void assertEqualsOnce(List<String> list, String value) {
        int count = 0;
        for (String s : list) {
            if (s.equals(value)) {
                count++;
            }
        }
        assertTrue("期望 " + value + " 恰好发信一次，实发 " + count + " 次", count == 1);
    }
}
