// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { describe, expect, it } from "vitest";

import { loadAuthSession, saveAuthSession } from "../auth-session";

describe("auth-session（refresh token 强杀耐久镜像）", () => {
  it("保存后可回读同一 token；null 保存即清除", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      saveAuthSession(dir, "refresh:abc123");
      expect(loadAuthSession(dir)).toBe("refresh:abc123");
      saveAuthSession(dir, null);
      expect(loadAuthSession(dir)).toBeNull();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("文件缺失/损坏 JSON/内容非法一律 null（fail-closed 走重新登录）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      expect(loadAuthSession(dir)).toBeNull();
      fs.writeFileSync(path.join(dir, "auth-session.json"), "{broken");
      expect(loadAuthSession(dir)).toBeNull();
      fs.writeFileSync(path.join(dir, "auth-session.json"), JSON.stringify({ refresh_token: 42 }));
      expect(loadAuthSession(dir)).toBeNull();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("重复保存覆盖旧值（单次轮换：新值必须顶掉旧值）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      saveAuthSession(dir, "old");
      saveAuthSession(dir, "new");
      expect(loadAuthSession(dir)).toBe("new");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
