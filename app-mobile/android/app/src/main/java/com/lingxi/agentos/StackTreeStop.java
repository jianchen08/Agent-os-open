package com.lingxi.agentos;

import java.util.ArrayList;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;

/**
 * 本机栈进程树清理（S5-A③ 重播种杀净面）。
 *
 * 真机实测教训：native 内核可幸存 TERM——强停后老内核占 9300 持自生成口令，
 * 新栈起不来=重播种自愈空转。两阶段契约：先 TERM 全树（保内核 graceful
 * shutdown = sidecar drain 既有语义，宽限窗由调用方持有）；宽限后对幸存者
 * KILL -9 升级，并如实回报全灭与否（杀不净=调用方告警依据，不静默假成功）。
 *
 * 进程面经 ProcessProbe 抽象：生产实现走壳内 sh pgrep/kill（进程同属本 app
 * UID，kill 权限自足；pgrep 模式 [x] 括号防自匹配——R3 教训）；JVM 测试注入
 * 假存活集，纯 Java 零 Android 依赖（SeedLayoutTest 同族）。
 */
public final class StackTreeStop {

    /** 进程存在/终止抽象（生产=sh pgrep/kill；测试=假存活集） */
    public interface ProcessProbe {
        /** @return 模式当前匹配的存活 pid 列表（无匹配=空表，不得返回 null） */
        List<String> alive(String pgrepPattern);

        /** 终止进程：force=false 走 SIGTERM，true 走 SIGKILL */
        void kill(String pid, boolean force);
    }

    /** 内核进程 pgrep 模式（[a] 括号防 pgrep 自匹配，与既有停栈面同款） */
    static final String PATTERN_KERNEL = "[a]gentos-kernel";
    /** 栈树残余 pgrep 模式：proot/python/sidecar 全在 /opt/agentos 路径下 */
    static final String PATTERN_STACK_TREE = "[o]pt/agentos";

    private final ProcessProbe probe;

    public StackTreeStop(ProcessProbe probe) {
        this.probe = probe;
    }

    /** 阶段一：全树 TERM（内核+栈树；宽限前唯一动作，保优雅停机语义）。 */
    public void termAll() {
        for (String pid : aliveAll()) {
            probe.kill(pid, false);
        }
    }

    /**
     * 单模式 TERM（停栈第一步只 TERM 内核：graceful 窗口内 proot 须存活转发，
     * 提前 TERM proot 会截断内核 sidecar drain——停栈次序契约由调用方编排）。
     */
    public void term(String pgrepPattern) {
        for (String pid : probe.alive(pgrepPattern)) {
            probe.kill(pid, false);
        }
    }

    /**
     * 阶段二：幸存者 KILL -9 升级。
     * @return 全灭与否（升级后仍有存活=false，调用方据此告警，不得假成功起栈）
     */
    public boolean escalateForce() {
        for (String pid : aliveAll()) {
            probe.kill(pid, true);
        }
        return aliveAll().isEmpty();
    }

    /** 双模式存活并集（同 pid 双命中去重，单发一次信号）。 */
    private List<String> aliveAll() {
        Set<String> pids = new LinkedHashSet<>();
        pids.addAll(probe.alive(PATTERN_KERNEL));
        pids.addAll(probe.alive(PATTERN_STACK_TREE));
        return new ArrayList<>(pids);
    }
}
