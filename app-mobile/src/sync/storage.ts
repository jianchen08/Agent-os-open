/**
 * 壳侧键值存储抽象 —— 引擎状态与配置应用面的持久化单点。
 *
 * 现状依据：app 侧可写配置面 = WebView localStorage（server_base_url/主题/壳
 * 去重集同面，local/remote 双模式通用、零桥开销）。凭据面独立走 Capacitor
 * Preferences（credential.ts），不与本面共享键空间（P6：凭据不入同步面）。
 */

export interface KvStore {
  get(key: string): string | null
  set(key: string, value: string): void
  remove(key: string): void
  /** 以 prefix 开头的全部键（字典序不承诺，调用方自行排序）。 */
  keysWithPrefix(prefix: string): string[]
}

function availableStorage(): Storage | null {
  try {
    const probe = "__lingxi_sync_probe__";
    window.localStorage.setItem(probe, "1");
    window.localStorage.removeItem(probe);
    return window.localStorage;
  } catch {
    return null;
  }
}

export class LocalStorageKv implements KvStore {
  private readonly storage: Storage | null;

  constructor(storage: Storage | null = availableStorage()) {
    this.storage = storage;
  }

  get available(): boolean {
    return this.storage !== null;
  }

  get(key: string): string | null {
    if (!this.storage) return null;
    try {
      return this.storage.getItem(key);
    } catch {
      return null;
    }
  }

  set(key: string, value: string): void {
    if (!this.storage) return;
    try {
      this.storage.setItem(key, value);
    } catch (err) {
      // 配额满/隐私模式：状态写失败不阻塞引擎循环，错误经 console 留痕可归因。
      console.warn("[lingxi-sync] kv set failed:", key, err);
    }
  }

  remove(key: string): void {
    if (!this.storage) return;
    try {
      this.storage.removeItem(key);
    } catch {
      // 移除失败无中间态危害（旧值残留等价于未生效的新状态）。
    }
  }

  keysWithPrefix(prefix: string): string[] {
    if (!this.storage) return [];
    const out: string[] = [];
    try {
      for (let i = 0; i < this.storage.length; i++) {
        const key = this.storage.key(i);
        if (key !== null && key.startsWith(prefix)) out.push(key);
      }
    } catch {
      return out;
    }
    return out;
  }
}

/** 键名单点（前缀面 + 各键），面板与引擎共同消费。 */
export const KEYS = {
  deviceId: "lingxi_sync.device_id",
  hlcState: "lingxi_sync.hlc_state",
  cursor: "lingxi_sync.cursor",
  assetPrefix: "lingxi_sync.asset.",
  appliedPrefix: "lingxi_sync.applied.",
  configPrefix: "lingxi_sync.config.",
  lastHeartbeat: "lingxi_sync.last_heartbeat",
} as const;
