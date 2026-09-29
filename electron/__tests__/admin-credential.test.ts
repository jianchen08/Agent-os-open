// ADR 2026-09-28 装机版自动登录：admin 凭据存档（生成/收编/回写/自愈）| @ci: frontend-test
import * as fs from "fs";
import * as os from "os";
import * as path from "path";

import { describe, expect, it, vi } from "vitest";

import {
  ensureAdminCredential,
  updateAdminCredential,
} from "../admin-credential";

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

/** fakeCipher：信封格式 enc(<json>)，与 ssMock 的编解码对齐 */
function fakeCipher(available = true) {
  return {
    isAvailable: () => available,
    encrypt: (plain: string) =>
      Buffer.from(`enc(${plain})`, "utf-8").toString("base64"),
    decrypt: (stored: string) => {
      const s = Buffer.from(stored, "base64").toString("utf-8");
      if (!s.startsWith("enc(") || !s.endsWith(")")) {
        throw new Error("not an encrypted payload");
      }
      return s.slice(4, -1);
    },
  };
}

function tmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), "admin-cred-"));
}

describe("ensureAdminCredential（幂等确保存档）", () => {
  it("无存档且无 ambient → 生成随机口令并落盘；再次调用返回同一份（幂等）", () => {
    const dir = tmpDir();
    try {
      const first = ensureAdminCredential(dir, null, fakeCipher());
      expect(first.username).toBe("admin");
      expect(first.password.length).toBeGreaterThanOrEqual(24);
      // 落盘为加密信封，明文口令不出现在文件里
      const raw = fs.readFileSync(
        path.join(dir, "admin-credential.json"),
        "utf-8",
      );
      expect(raw).not.toContain(first.password);

      const second = ensureAdminCredential(dir, null, fakeCipher());
      expect(second).toEqual(first);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("ambient AGENTOS_ADMIN_PASSWORD 显式设置 → 收编为存档值（恢复通道语义）", () => {
    const dir = tmpDir();
    try {
      const cred = ensureAdminCredential(dir, "  operator-pw  ", fakeCipher());
      expect(cred.password).toBe("operator-pw");
      // 收编持久化：换 ambient 为空后仍读回同一值
      const again = ensureAdminCredential(dir, null, fakeCipher());
      expect(again.password).toBe("operator-pw");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("存档损坏（非法 JSON）→ 视为无存档重新生成（自愈：内核重置语义随后对齐）", () => {
    const dir = tmpDir();
    try {
      fs.writeFileSync(
        path.join(dir, "admin-credential.json"),
        "{not-json",
        "utf-8",
      );
      const cred = ensureAdminCredential(dir, null, fakeCipher());
      expect(cred.username).toBe("admin");
      expect(cred.password.length).toBeGreaterThan(0);
      // 损坏文件已被合法存档替换
      const again = ensureAdminCredential(dir, null, fakeCipher());
      expect(again).toEqual(cred);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("两次生成的口令不同（密码学随机，非固定默认口令）", () => {
    const a = ensureAdminCredential(tmpDir(), null, fakeCipher());
    const b = ensureAdminCredential(tmpDir(), null, fakeCipher());
    expect(a.password).not.toBe(b.password);
  });
});

describe("updateAdminCredential（改密回写）", () => {
  it("存档账号一致（admin）→ 接受回写并持久化", () => {
    const dir = tmpDir();
    try {
      ensureAdminCredential(dir, null, fakeCipher());
      expect(updateAdminCredential(dir, "admin", "new-pw", fakeCipher())).toBe(
        true,
      );
      const cred = ensureAdminCredential(dir, null, fakeCipher());
      expect(cred.password).toBe("new-pw");
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("非存档账号（普通用户）→ 拒收，存档不变", () => {
    const dir = tmpDir();
    try {
      const before = ensureAdminCredential(dir, null, fakeCipher());
      expect(updateAdminCredential(dir, "alice", "her-pw", fakeCipher())).toBe(
        false,
      );
      expect(ensureAdminCredential(dir, null, fakeCipher())).toEqual(before);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("无存档（dev 形态）→ 拒收且不创建存档", () => {
    const dir = tmpDir();
    try {
      expect(updateAdminCredential(dir, "admin", "pw", fakeCipher())).toBe(
        false,
      );
      expect(fs.existsSync(path.join(dir, "admin-credential.json"))).toBe(
        false,
      );
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("空口令/空用户名 → 参数拒收", () => {
    const dir = tmpDir();
    try {
      ensureAdminCredential(dir, null, fakeCipher());
      expect(updateAdminCredential(dir, "admin", "", fakeCipher())).toBe(false);
      expect(updateAdminCredential(dir, "", "pw", fakeCipher())).toBe(false);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});
