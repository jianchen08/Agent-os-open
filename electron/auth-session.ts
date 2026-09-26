/**
 * 认证会话镜像（主进程侧持久化）：refresh token 的强杀耐久备份。
 *
 * 渲染进程的 localStorage 走 Chromium Storage Service 批量提交，进程被
 * 强杀（任务管理器结束任务/断电）时最近的轮换写入可能尚未落盘——而服务端
 * 的单次轮换已作废旧值，重启后 stored token 被拒 → 每次都要重新登录。
 * 主进程 fs.writeFileSync 立即落入 OS 页缓存，进程强杀不丢。
 *
 * 落盘格式优先走 safeStorage 加密信封（Windows DPAPI / macOS Keychain /
 * Linux libsecret，OS 级当前用户绑定）；加密不可用时回落明文 0600（信任域
 * 与 localStorage 相同：本机同用户），可用性恢复后读取旧明文即惰性重加密。
 * 删除文件 = 全端登出（与清除 localStorage 等效）。
 */
import * as fs from "fs";
import * as path from "path";

import { safeStorage } from "electron";

const AUTH_SESSION_FILE = "auth-session.json";

/** 加密信封版本；格式演进只增版本号，读取按版本分发。 */
const ENVELOPE_VERSION = 1;

/** 存储信封形态：加密数据以 base64 承载。 */
interface StoredEnvelope {
  version: number;
  cipher: "safeStorage";
  data: string;
}

/** 旧版明文形态（safeStorage 引入前的既有文件，读取时惰性迁移）。 */
interface LegacyStored {
  refresh_token?: unknown;
}

/**
 * 加密适配面：主进程传 electron safeStorage 真身；测试注入替身。
 * decrypt 对无法解密的数据抛错（fail-closed 由调用方落 null）。
 */
export interface TokenCipher {
  isAvailable(): boolean;
  encrypt(plain: string): string;
  decrypt(stored: string): string;
}

type ElectronSafeStorage = {
  isEncryptionAvailable(): boolean;
  encryptString(plain: string): Buffer;
  decryptString(data: Buffer): string;
};

/** 惰性适配 safeStorage；纯 Node 环境（未 mock electron）下为 undefined。 */
const ss = safeStorage as ElectronSafeStorage | undefined;

function resolveElectronCipher(): TokenCipher | null {
  if (!ss) {
    return null;
  }
  return {
    isAvailable: () => {
      try {
        return ss.isEncryptionAvailable();
      } catch {
        return false;
      }
    },
    encrypt: (plain) => ss.encryptString(plain).toString("base64"),
    decrypt: (stored) => ss.decryptString(Buffer.from(stored, "base64")),
  };
}

let warnedPlaintextFallback = false;

function isUsableToken(token: unknown): token is string {
  return typeof token === "string" && token.length > 0;
}

/**
 * 保存 refresh token（null = 清除）。同步写——调用点在 IPC handler 内。
 * 加密不可用/加密失败回落明文：耐久备份优先于静态加密（本机同用户信任域）。
 */
export function saveAuthSession(
  userDataDir: string,
  refreshToken: string | null,
  cipher?: TokenCipher | null,
): void {
  const file = path.join(userDataDir, AUTH_SESSION_FILE);
  if (refreshToken === null) {
    try {
      fs.rmSync(file, { force: true });
    } catch {
      // 文件本就不存在，视为已清除
    }
    return;
  }
  const active = cipher === undefined ? resolveElectronCipher() : cipher;
  let payload: StoredEnvelope | LegacyStored;
  if (active?.isAvailable()) {
    try {
      payload = {
        version: ENVELOPE_VERSION,
        cipher: "safeStorage",
        data: active.encrypt(refreshToken),
      };
    } catch {
      payload = { refresh_token: refreshToken };
    }
  } else {
    payload = { refresh_token: refreshToken };
    if (active !== null && !warnedPlaintextFallback) {
      warnedPlaintextFallback = true;
      console.warn(
        "[auth-session] safeStorage 不可用，refresh token 镜像回落明文 0600",
      );
    }
  }
  fs.mkdirSync(userDataDir, { recursive: true });
  fs.writeFileSync(file, JSON.stringify(payload), {
    encoding: "utf-8",
    mode: 0o600,
  });
}

/**
 * 读取 refresh token；文件缺失/损坏/内容非法/解密失败一律 null（fail-closed
 * 走重新登录）。读到旧版明文且加密可用时惰性重加密回写（后端可能长期不轮换，
 * 不能等下一次轮换才迁移）。
 */
export function loadAuthSession(
  userDataDir: string,
  cipher?: TokenCipher | null,
): string | null {
  let raw: unknown;
  try {
    raw = JSON.parse(
      fs.readFileSync(path.join(userDataDir, AUTH_SESSION_FILE), "utf-8"),
    );
  } catch {
    return null;
  }
  const active = cipher === undefined ? resolveElectronCipher() : cipher;
  const envelope = raw as Partial<StoredEnvelope> & LegacyStored;
  if (
    envelope?.version === ENVELOPE_VERSION &&
    envelope.cipher === "safeStorage" &&
    isUsableToken(envelope.data)
  ) {
    if (!active?.isAvailable()) {
      return null;
    }
    try {
      return active.decrypt(envelope.data);
    } catch {
      return null;
    }
  }
  if (!isUsableToken(envelope?.refresh_token)) {
    return null;
  }
  const token = envelope.refresh_token;
  // 惰性迁移：明文 → 加密信封；迁移失败不破坏本次登录恢复
  if (active?.isAvailable()) {
    try {
      saveAuthSession(userDataDir, token, active);
    } catch {
      // 保留明文镜像，下次读取再迁移
    }
  }
  return token;
}
