/**
 * 装机版 admin 凭据存档（主进程持有，ADR 2026-09-28-packaged-auto-admin-login-and-login-modal）：
 *
 * 自动登录的口令事实源。首次生成密码学随机口令（或收编 ambient
 * AGENTOS_ADMIN_PASSWORD——操作员恢复通道语义保持），safeStorage 加密信封
 * 落盘 userData（Windows DPAPI 当前用户绑定）；加密不可用回落明文 0600，
 * 信任域与 auth-session 相同（本机同用户）。存档每次随内核 spawn 注入
 * AGENTOS_ADMIN_PASSWORD：内核「环境变量与当前口令不符即重置」的既有语义
 * 在此成为自愈通道——存档损坏/丢失即重新生成，下次启动内核自动对齐；
 * 用户经应用内改密后由渲染进程 IPC（auth:admin-credential:sync）回写存档，
 * 注入值恒与真实口令一致。
 */
import { randomBytes } from "crypto";
import * as fs from "fs";
import * as path from "path";

import { resolveElectronCipher, type TokenCipher } from "./auth-session";

const ADMIN_CREDENTIAL_FILE = "admin-credential.json";

/** 加密信封版本；格式演进只增版本号，读取按版本分发。 */
const ENVELOPE_VERSION = 1;

/** 存档形态（加密前明文）。 */
interface StoredCredential {
  username: string;
  password: string;
}

/** 加密信封形态：credential JSON 以 base64 承载。 */
interface StoredEnvelope {
  version: number;
  cipher: "safeStorage";
  data: string;
}

/** 旧版明文形态（safeStorage 引入前的既有文件，读取时惰性迁移）。 */
interface LegacyStored extends StoredCredential {}

export interface AdminCredential {
  username: string;
  password: string;
}

function credentialFile(userDataDir: string): string {
  return path.join(userDataDir, ADMIN_CREDENTIAL_FILE);
}

function isUsableCredential(v: unknown): v is StoredCredential {
  const c = v as Partial<StoredCredential> | null;
  return (
    typeof c?.username === "string" &&
    c.username.length > 0 &&
    typeof c.password === "string" &&
    c.password.length > 0
  );
}

function encryptPayload(
  credential: StoredCredential,
  cipher: TokenCipher,
): StoredEnvelope | LegacyStored {
  if (cipher.isAvailable()) {
    try {
      return {
        version: ENVELOPE_VERSION,
        cipher: "safeStorage",
        data: cipher.encrypt(JSON.stringify(credential)),
      };
    } catch {
      // 加密失败回落明文（与 auth-session 同理由：耐久备份优先于静态加密）
    }
  }
  return { ...credential };
}

function writeCredential(
  userDataDir: string,
  credential: StoredCredential,
  cipher: TokenCipher,
): void {
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(
    credentialFile(userDataDir),
    JSON.stringify(encryptPayload(credential, cipher)),
    {
      encoding: "utf-8",
      mode: 0o600,
    },
  );
}

/**
 * 读取存档；文件缺失/损坏/解密失败一律 null（调用方按"无存档"重新生成——
 * 存档不可读时新生成的口令会经内核重置语义对齐，自动登录不因此失能）。
 */
function readCredential(
  userDataDir: string,
  cipher: TokenCipher,
): StoredCredential | null {
  let raw: unknown;
  try {
    raw = JSON.parse(fs.readFileSync(credentialFile(userDataDir), "utf-8"));
  } catch {
    return null;
  }
  const envelope = raw as Partial<StoredEnvelope> & LegacyStored;
  if (
    envelope?.version === ENVELOPE_VERSION &&
    envelope.cipher === "safeStorage" &&
    typeof envelope.data === "string" &&
    envelope.data.length > 0
  ) {
    if (!cipher.isAvailable()) {
      return null;
    }
    try {
      const parsed: unknown = JSON.parse(cipher.decrypt(envelope.data));
      return isUsableCredential(parsed) ? parsed : null;
    } catch {
      return null;
    }
  }
  return isUsableCredential(envelope)
    ? { username: envelope.username, password: envelope.password }
    : null;
}

/**
 * 确保 admin 凭据存档存在并返回（幂等）：
 * - ambient AGENTOS_ADMIN_PASSWORD 显式设置 → 收编为存档值（与内核本次启动
 *   实际生效的口令一致，恢复通道与存档不再分叉）；
 * - 已有合法存档 → 原样返回；
 * - 无存档 → 生成密码学随机口令并落盘。
 */
export function ensureAdminCredential(
  userDataDir: string,
  ambient?: string | null,
  cipher?: TokenCipher | null,
): AdminCredential {
  const active = cipher === undefined ? resolveElectronCipher() : cipher;
  const usable = active ?? {
    isAvailable: () => false,
    encrypt: (plain: string) => plain,
    decrypt: (stored: string) => stored,
  };
  const ambientPassword = ambient?.trim();
  if (ambientPassword) {
    const credential: StoredCredential = {
      username: "admin",
      password: ambientPassword,
    };
    writeCredential(userDataDir, credential, usable);
    return credential;
  }
  const stored = readCredential(userDataDir, usable);
  if (stored) {
    return stored;
  }
  const generated: StoredCredential = {
    username: "admin",
    password: randomBytes(24).toString("base64url"),
  };
  writeCredential(userDataDir, generated, usable);
  return generated;
}

/**
 * 渲染进程改密后的存档回写。仅当存档账号与改密账号一致（内置 admin）时
 * 接受——普通用户的口令不入本存档；无存档（dev）同样拒收。
 */
export function updateAdminCredential(
  userDataDir: string,
  username: string,
  password: string,
  cipher?: TokenCipher | null,
): boolean {
  if (
    typeof username !== "string" ||
    !username ||
    typeof password !== "string" ||
    !password
  ) {
    return false;
  }
  const active = cipher === undefined ? resolveElectronCipher() : cipher;
  const usable = active ?? {
    isAvailable: () => false,
    encrypt: (plain: string) => plain,
    decrypt: (stored: string) => stored,
  };
  const stored = readCredential(userDataDir, usable);
  if (!stored || stored.username !== username) {
    return false;
  }
  writeCredential(userDataDir, { username: stored.username, password }, usable);
  return true;
}
