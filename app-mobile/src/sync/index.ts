/**
 * 壳侧同步模块装配入口 —— MainActivity 注入后自启（幂等护栏防重复注入）。
 *
 * 装配链：localStorage 状态面 + Capacitor Preferences 凭据面 + relay 客户端 +
 * 引擎（已配对自动续跑）+ 状态面板（浮动入口）。入网（登录即入网，ADR
 * 2026-11-02-login-as-enrollment）= 前端登录成功后调 joinWithAuth(jwt, base)；
 * 基址解析链与 shell-push 同契约：调用方显式基址 > 用户设置
 * localStorage.server_base_url > 构建期注入 window.LINGXI_SHELL_CONFIG.apiBase。
 */

import { CredentialStore } from "./credential";
import { ConfigStore } from "./configStore";
import { SyncPanel, injectChipStyle } from "./panel";
import { RelayClient } from "./relayClient";
import { LocalStorageKv } from "./storage";
import { SyncEngine, type SyncCredential } from "./syncEngine";

export interface LingxiSyncApi {
  engine: SyncEngine
  panel: SyncPanel
  /** 登录入网：前端登录成功后调用（admin JWT 直签；base=当前连接 api 基址）。 */
  joinWithAuth: (jwt: string, apiBase?: string) => Promise<SyncCredential>
  open: () => void
  close: () => void
}

function resolveApiBase(): string {
  try {
    const userSetting = window.localStorage.getItem("server_base_url");
    if (userSetting && userSetting.trim()) return userSetting.trim();
  } catch {
    // 本地存储不可用时直接落构建期注入值。
  }
  const injected = (globalThis as { LINGXI_SHELL_CONFIG?: { apiBase?: unknown } })
    .LINGXI_SHELL_CONFIG;
  if (typeof injected?.apiBase === "string" && injected.apiBase) return injected.apiBase;
  return "";
}

function relayBaseOf(apiBase: string): string {
  if (!apiBase) return "/ext/sync_relay";
  return apiBase.replace(/\/+$/, "") + "/ext/sync_relay";
}

function boot(): LingxiSyncApi | null {
  if ((globalThis as { __LINGXI_SHELL_SYNC__?: boolean }).__LINGXI_SHELL_SYNC__) {
    return (globalThis as { __LINGXI_SYNC_API__?: LingxiSyncApi }).__LINGXI_SYNC_API__ ?? null;
  }
  (globalThis as { __LINGXI_SHELL_SYNC__?: boolean }).__LINGXI_SHELL_SYNC__ = true;

  const kv = new LocalStorageKv();
  if (!kv.available) {
    console.warn("[lingxi-sync] localStorage 不可用，同步模块停用（状态面无法持久化）");
    return null;
  }
  const credential = new CredentialStore(kv);
  const store = new ConfigStore(kv);
  const client = new RelayClient();
  const engine = new SyncEngine({
    credential,
    store,
    client,
    joinRelayUrl: () => relayBaseOf(resolveApiBase()),
  });
  const panel = new SyncPanel({ engine, credentialBackend: () => credential.backend });

  void engine
    .start()
    .then((started) => {
      if (started) console.info("[lingxi-sync] 已配对，同步引擎续跑");
    })
    .catch((err) => console.warn("[lingxi-sync] 引擎启动失败:", err));

  if (typeof document !== "undefined" && document.body) {
    injectChipStyle(document.body);
    panel.mount(document.body);
  } else {
    console.warn("[lingxi-sync] document 未就绪，仅暴露 window.LingxiSync 编程入口");
  }

  const api: LingxiSyncApi = {
    engine,
    panel,
    joinWithAuth: (jwt, apiBase) => {
      const base = apiBase?.trim();
      return base ? engine.joinWithAuth(jwt, relayBaseOf(base)) : engine.joinWithAuth(jwt);
    },
    open: () => void panel.open(),
    close: () => panel.close(),
  };
  (globalThis as { __LINGXI_SYNC_API__?: LingxiSyncApi }).__LINGXI_SYNC_API__ = api;
  return api;
}

export const lingxiSync: LingxiSyncApi | null =
  typeof window !== "undefined" ? boot() : null;

if (typeof window !== "undefined") {
  (window as unknown as { LingxiSync?: LingxiSyncApi }).LingxiSync = lingxiSync ?? undefined;
}
