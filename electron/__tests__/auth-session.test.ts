// @feature: FP-0.2.三 宿主接入 | @ci: frontend-test
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { describe, expect, it, vi } from "vitest";

import { loadAuthSession, saveAuthSession, type TokenCipher } from "../auth-session";

const ssMock = vi.hoisted(() => ({
  isEncryptionAvailable: vi.fn((): boolean => true),
  encryptString: vi.fn((plain: string): Buffer =>
    Buffer.from(`enc(${plain})`, "utf-8"),
  ),
  decryptString: vi.fn((data: Buffer): string => {
    const s = data.toString("utf-8");
    if (!s.startsWith("enc(") || !s.endsWith(")")) {
      throw new Error("not an encrypted payload");
    }
    return s.slice(4, -1);
  }),
}));

vi.mock("electron", () => ({ safeStorage: ssMock }));

function fileIn(dir: string): string {
  return path.join(dir, "auth-session.json");
}

function fakeCipher(available = true): TokenCipher {
  return {
    isAvailable: () => available,
    encrypt: (plain) => Buffer.from(`enc(${plain})`, "utf-8").toString("base64"),
    decrypt: (stored) => {
      const s = Buffer.from(stored, "base64").toString("utf-8");
      if (!s.startsWith("enc(") || !s.endsWith(")")) {
        throw new Error("not an encrypted payload");
      }
      return s.slice(4, -1);
    },
  };
}

describe("auth-session（refresh token 强杀耐久镜像）", () => {
  it("保存后可回读同一 token；null 保存即清除（加密信封路径）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      saveAuthSession(dir, "refresh:abc123", fakeCipher());
      // 落盘为加密信封，明文不出现在文件里
      const raw = fs.readFileSync(fileIn(dir), "utf-8");
      expect(raw).not.toContain("refresh:abc123");
      expect(JSON.parse(raw)).toMatchObject({
        version: 1,
        cipher: "safeStorage",
      });
      expect(loadAuthSession(dir, fakeCipher())).toBe("refresh:abc123");
      saveAuthSession(dir, null, fakeCipher());
      expect(loadAuthSession(dir, fakeCipher())).toBeNull();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("文件缺失/损坏 JSON/内容非法一律 null（fail-closed 走重新登录）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      expect(loadAuthSession(dir, fakeCipher())).toBeNull();
      fs.writeFileSync(fileIn(dir), "{broken");
      expect(loadAuthSession(dir, fakeCipher())).toBeNull();
      fs.writeFileSync(fileIn(dir), JSON.stringify({ refresh_token: 42 }));
      expect(loadAuthSession(dir, fakeCipher())).toBeNull();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("重复保存覆盖旧值（单次轮换：新值必须顶掉旧值）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      saveAuthSession(dir, "old", fakeCipher());
      saveAuthSession(dir, "new", fakeCipher());
      expect(loadAuthSession(dir, fakeCipher())).toBe("new");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("safeStorage 不可用时回落明文 0600 且照常回读（耐久优先）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
      saveAuthSession(dir, "plain-fallback", fakeCipher(false));
      const raw = JSON.parse(fs.readFileSync(fileIn(dir), "utf-8")) as {
        refresh_token?: string;
      };
      expect(raw.refresh_token).toBe("plain-fallback");
      expect(loadAuthSession(dir, fakeCipher(false))).toBe("plain-fallback");
      expect(warn).toHaveBeenCalled();
      warn.mockRestore();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("旧版明文文件读取即惰性迁移为加密信封（后端可能长期不轮换）", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      fs.writeFileSync(
        fileIn(dir),
        JSON.stringify({ refresh_token: "legacy-plain" }),
      );
      expect(loadAuthSession(dir, fakeCipher())).toBe("legacy-plain");
      const after = JSON.parse(
        fs.readFileSync(fileIn(dir), "utf-8"),
      ) as { version?: number; refresh_token?: string };
      expect(after.version).toBe(1);
      expect(after.refresh_token).toBeUndefined();
      // 迁移后仍可解密回读
      expect(loadAuthSession(dir, fakeCipher())).toBe("legacy-plain");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("信封解密失败（换机/密钥环失效）一律 null，不误读脏数据", () => {
    const dir = fs.mkdtempSync(path.join(os.tmpdir(), "as-"));
    try {
      saveAuthSession(dir, "secret", fakeCipher());
      // 模拟密钥环失效：文件里塞非本机密文
      fs.writeFileSync(
        fileIn(dir),
        JSON.stringify({ version: 1, cipher: "safeStorage", data: "bGQ=" }),
      );
      expect(loadAuthSession(dir, fakeCipher())).toBeNull();
      // 加密能力消失同样 fail-closed
      expect(loadAuthSession(dir, fakeCipher(false))).toBeNull();
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
