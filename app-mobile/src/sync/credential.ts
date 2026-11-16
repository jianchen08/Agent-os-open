/**
 * 配对凭据存储与 join 状态机 —— 凭据永不入同步面（P6），兑换单向推进。
 *
 * 通道选择：Capacitor Preferences（原生 SharedPreferences，跨 WebView 数据
 * 清理存活，@capacitor/preferences 随包自动注册）优先；插件缺席（旧包/浏览器
 * 调试）降级 localStorage 同键同值。通道在构造时探测一次并锁定，读写不分裂。
 *
 * 状态机（与 sync_agent 的 join/reset 语义同构）：
 *   unpaired --join 成功--> paired（先落凭据再作废旧配对状态：游标/资产账本/
 *   已应用账本清空，HLC 时钟与设备 UUID 保留——跨配对单调续接）；
 *   paired --unpair--> unpaired（凭据与配对状态同清）；
 *   兑换失败（错码/过期/已用）任何状态不变（零副作用，relay 一次即拒）。
 */

import { KEYS, type KvStore } from "./storage";

export interface SyncCredential {
  readonly relayUrl: string
  readonly deviceId: string
  readonly deviceToken: string
  readonly tenantId: string
  readonly tier: string
  readonly appVersion: string
  readonly pairedAt: string
}

/** Capacitor Preferences 插件的最小运行时面（global Plugins 注册表直取）。 */
interface PreferencesPlugin {
  get(opts: { key: string }): Promise<{ value?: string | null }>
  set(opts: { key: string; value: string }): Promise<void>
  remove(opts: { key: string }): Promise<void>
}

function resolvePreferencesPlugin(): PreferencesPlugin | null {
  const plugins = (globalThis as { Capacitor?: { Plugins?: Record<string, unknown> } }).Capacitor
    ?.Plugins;
  const plugin = plugins?.Preferences;
  return plugin && typeof (plugin as PreferencesPlugin).get === "function"
    ? (plugin as PreferencesPlugin)
    : null;
}

export const CREDENTIAL_KEY = "lingxi_sync_credential";

export class CredentialStore {
  private readonly prefs: PreferencesPlugin | null;
  private readonly fallback: KvStore;

  constructor(fallback: KvStore, prefs: PreferencesPlugin | null = resolvePreferencesPlugin()) {
    this.prefs = prefs;
    this.fallback = fallback;
  }

  /** 凭据驻留通道（诊断/面板展示用）。 */
  get backend(): "preferences" | "local_storage" {
    return this.prefs ? "preferences" : "local_storage";
  }

  async load(): Promise<SyncCredential | null> {
    let raw: string | null = null;
    if (this.prefs) {
      try {
        raw = (await this.prefs.get({ key: CREDENTIAL_KEY })).value ?? null;
      } catch (err) {
        console.warn("[lingxi-sync] credential read failed:", err);
      }
    }
    if (raw === null) raw = this.fallback.get(CREDENTIAL_KEY);
    if (!raw) return null;
    return parseCredential(raw);
  }

  async save(credential: SyncCredential): Promise<void> {
    // 线格式（snake_case，与 relay join 响应字段同名）：save/parse 同一形态，往返不漂移。
    const raw = JSON.stringify({
      relay_url: credential.relayUrl,
      device_id: credential.deviceId,
      device_token: credential.deviceToken,
      tenant_id: credential.tenantId,
      tier: credential.tier,
      app_version: credential.appVersion,
      paired_at: credential.pairedAt,
    });
    if (this.prefs) {
      try {
        await this.prefs.set({ key: CREDENTIAL_KEY, value: raw });
        return;
      } catch (err) {
        // 原生写失败降级本页存储：凭据丢失会静默解除配对，可用的弱持久面好于无凭据。
        console.warn("[lingxi-sync] credential native save failed, falling back:", err);
      }
    }
    this.fallback.set(CREDENTIAL_KEY, raw);
  }

  async clear(): Promise<void> {
    if (this.prefs) {
      try {
        await this.prefs.remove({ key: CREDENTIAL_KEY });
      } catch (err) {
        console.warn("[lingxi-sync] credential native clear failed:", err);
      }
    }
    this.fallback.remove(CREDENTIAL_KEY);
  }
}

function parseCredential(raw: string): SyncCredential | null {
  try {
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (
      typeof value.relay_url !== "string" ||
      typeof value.device_id !== "string" ||
      typeof value.device_token !== "string" ||
      !value.relay_url ||
      !value.device_id ||
      !value.device_token
    ) {
      return null;
    }
    return {
      relayUrl: value.relay_url,
      deviceId: value.device_id,
      deviceToken: value.device_token,
      tenantId: typeof value.tenant_id === "string" ? value.tenant_id : "",
      tier: typeof value.tier === "string" ? value.tier : "",
      appVersion: typeof value.app_version === "string" ? value.app_version : "",
      pairedAt: typeof value.paired_at === "string" ? value.paired_at : "",
    };
  } catch {
    return null;
  }
}
