// @ci: frontend-test
/**
 * diag-log 纯函数层 + stderr 落盘单测（node 环境，真实 fs 走 tmpdir）。
 *
 * 覆盖口径：断行为（输入→输出/落盘副作用）。观测面契约——轮转只进不退
 * （超限才轮转）、写失败静默（不反噬主流程）。
 */

import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { describe, expect, it } from "vitest";

import {
  DIAG_LOG_MAX_BYTES,
  diagLogPaths,
  shouldRotateDiagLog,
} from "../diag-log";
import { KERNEL_STDERR_MAX_BYTES, appendKernelStderr } from "../kernel-manager";

describe("shouldRotateDiagLog", () => {
  it("size below cap keeps current file", () => {
    expect(shouldRotateDiagLog(DIAG_LOG_MAX_BYTES - 1)).toBe(false);
  });

  it("size at cap rotates (boundary inclusive)", () => {
    expect(shouldRotateDiagLog(DIAG_LOG_MAX_BYTES)).toBe(true);
    expect(shouldRotateDiagLog(DIAG_LOG_MAX_BYTES + 1)).toBe(true);
  });
});

describe("diagLogPaths", () => {
  it("names main log and single .old generation", () => {
    const { current, old } = diagLogPaths(path.join("u", "logs"));
    expect(current).toBe(path.join("u", "logs", "electron-main.log"));
    expect(old).toBe(path.join("u", "logs", "electron-main.log.old"));
  });
});

describe("appendKernelStderr", () => {
  it("appends chunks to kernel-stderr.log (panic trace survives)", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "agentos-kstderr-"));
    try {
      appendKernelStderr(dir, "thread 'main' panicked at engine\n");
      appendKernelStderr(dir, "note: run with RUST_BACKTRACE=1\n");
      const file = path.join(dir, "kernel-stderr.log");
      const content = fs.readFileSync(file, "utf-8");
      expect(content).toContain("panicked at engine");
      expect(content).toContain("RUST_BACKTRACE");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("rotates to .old once the current file crosses the cap", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "agentos-kstderr-"));
    try {
      const big = "x".repeat(KERNEL_STDERR_MAX_BYTES);
      appendKernelStderr(dir, big);
      appendKernelStderr(dir, "fresh-after-rotate");
      expect(
        fs.existsSync(path.join(dir, "kernel-stderr.log.old")),
        "超限文件轮转为 .old",
      ).toBe(true);
      const current = fs.readFileSync(
        path.join(dir, "kernel-stderr.log"),
        "utf-8",
      );
      expect(current).toBe("fresh-after-rotate");
      expect(
        fs.statSync(path.join(dir, "kernel-stderr.log.old")).size,
      ).toBeGreaterThanOrEqual(KERNEL_STDERR_MAX_BYTES);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
