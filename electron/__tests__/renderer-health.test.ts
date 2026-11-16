// @feature: FP-0.2.可观测性 渲染进程健康观测 | @ci: frontend-test
/**
 * 渲染进程健康观测测试（renderer-health.ts，修 G4 观测缺口）。
 *
 * 行为契约：
 *  - render-process-gone：reason/exitCode/processId 全量进诊断行，logDiag 与
 *    console.warn 都留痕（证据面先行）；
 *  - unresponsive/responsive：挂死与恢复都留痕（恢复行使下次挂死可对账）；
 *  - processId 取不到（null）→ 落 "?"，观测面不阻断、不抛出；
 *  - 不自动重启、不弹窗：处理函数只做日志，无其他副作用。
 *
 * 不 mock 任何外部模块：logDiag 经参数注入，console.warn 用 spy 断言。
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import {
  reportRenderProcessGone,
  reportWebContentsResponsive,
  reportWebContentsUnresponsive,
} from "../renderer-health";

const diagLines: string[] = [];
const diag = (line: string): void => {
  diagLines.push(line);
};

afterEach(() => {
  diagLines.length = 0;
  vi.restoreAllMocks();
});

describe("reportRenderProcessGone", () => {
  it("reason/exitCode/processId 全量落诊断行 + console.warn", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    reportRenderProcessGone({ reason: "crashed", exitCode: -1073741819 }, 4321, diag);
    expect(diagLines).toEqual([
      "[renderer] render-process-gone reason=crashed exitCode=-1073741819 processId=4321",
    ]);
    expect(warn).toHaveBeenCalledTimes(1);
    expect(String(warn.mock.calls[0]![0])).toContain("渲染进程消失");
  });

  it("processId 取不到（null）→ 落 ?，不抛出（观测面不阻断）", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(() =>
      reportRenderProcessGone({ reason: "oom", exitCode: 0 }, null, diag),
    ).not.toThrow();
    expect(diagLines[0]).toBe("[renderer] render-process-gone reason=oom exitCode=0 processId=?");
  });

  it("两组有区分度输入 → 行内容随详情变化（防拟合）", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    reportRenderProcessGone({ reason: "launch-failed", exitCode: 3221225786 }, 7, diag);
    reportRenderProcessGone({ reason: "clean-exit", exitCode: 0 }, 9, diag);
    expect(diagLines[0]).toContain("reason=launch-failed");
    expect(diagLines[0]).toContain("exitCode=3221225786");
    expect(diagLines[1]).toContain("reason=clean-exit");
    expect(diagLines[1]).toContain("processId=9");
  });
});

describe("reportWebContentsUnresponsive / Responsive", () => {
  it("挂死与恢复都留 diag 行 + console.warn，行含进程号", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    reportWebContentsUnresponsive(1001, diag);
    reportWebContentsResponsive(1001, diag);
    expect(diagLines).toEqual([
      "[renderer] webContents unresponsive processId=1001",
      "[renderer] webContents responsive processId=1001",
    ]);
    expect(warn).toHaveBeenCalledTimes(2);
  });

  it("processId 缺失 → ? 占位不抛出", () => {
    vi.spyOn(console, "warn").mockImplementation(() => {});
    reportWebContentsUnresponsive(null, diag);
    expect(diagLines[0]).toBe("[renderer] webContents unresponsive processId=?");
  });
});
