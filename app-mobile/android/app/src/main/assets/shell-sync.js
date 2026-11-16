/*
 * 由 scripts/build-sync.mjs 生成（源单点 src/sync/），勿手改；
 * MainActivity 页面加载完成后注入，shell-push.js 同款机制。
 */
"use strict";
(() => {
  var __defProp = Object.defineProperty;
  var __defNormalProp = (obj, key, value) => key in obj ? __defProp(obj, key, { enumerable: true, configurable: true, writable: true, value }) : obj[key] = value;
  var __publicField = (obj, key, value) => __defNormalProp(obj, typeof key !== "symbol" ? key + "" : key, value);

  // src/sync/credential.ts
  function resolvePreferencesPlugin() {
    const plugins = globalThis.Capacitor?.Plugins;
    const plugin = plugins?.Preferences;
    return plugin && typeof plugin.get === "function" ? plugin : null;
  }
  var CREDENTIAL_KEY = "lingxi_sync_credential";
  var CredentialStore = class {
    constructor(fallback, prefs = resolvePreferencesPlugin()) {
      __publicField(this, "prefs");
      __publicField(this, "fallback");
      this.prefs = prefs;
      this.fallback = fallback;
    }
    /** 凭据驻留通道（诊断/面板展示用）。 */
    get backend() {
      return this.prefs ? "preferences" : "local_storage";
    }
    async load() {
      let raw = null;
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
    async save(credential) {
      const raw = JSON.stringify({
        relay_url: credential.relayUrl,
        device_id: credential.deviceId,
        device_token: credential.deviceToken,
        tenant_id: credential.tenantId,
        tier: credential.tier,
        app_version: credential.appVersion,
        paired_at: credential.pairedAt
      });
      if (this.prefs) {
        try {
          await this.prefs.set({ key: CREDENTIAL_KEY, value: raw });
          return;
        } catch (err) {
          console.warn("[lingxi-sync] credential native save failed, falling back:", err);
        }
      }
      this.fallback.set(CREDENTIAL_KEY, raw);
    }
    async clear() {
      if (this.prefs) {
        try {
          await this.prefs.remove({ key: CREDENTIAL_KEY });
        } catch (err) {
          console.warn("[lingxi-sync] credential native clear failed:", err);
        }
      }
      this.fallback.remove(CREDENTIAL_KEY);
    }
  };
  function parseCredential(raw) {
    try {
      const value = JSON.parse(raw);
      if (typeof value.relay_url !== "string" || typeof value.device_id !== "string" || typeof value.device_token !== "string" || !value.relay_url || !value.device_id || !value.device_token) {
        return null;
      }
      return {
        relayUrl: value.relay_url,
        deviceId: value.device_id,
        deviceToken: value.device_token,
        tenantId: typeof value.tenant_id === "string" ? value.tenant_id : "",
        tier: typeof value.tier === "string" ? value.tier : "",
        appVersion: typeof value.app_version === "string" ? value.app_version : "",
        pairedAt: typeof value.paired_at === "string" ? value.paired_at : ""
      };
    } catch {
      return null;
    }
  }

  // src/sync/hlc.ts
  function compareHlc(a, b) {
    if (a.physicalMs !== b.physicalMs) return a.physicalMs - b.physicalMs;
    if (a.logical !== b.logical) return a.logical - b.logical;
    if (a.deviceId < b.deviceId) return -1;
    if (a.deviceId > b.deviceId) return 1;
    return 0;
  }
  function assertNonNegativeInt(value, name) {
    if (typeof value === "boolean" || typeof value !== "number" || !Number.isSafeInteger(value) || value < 0) {
      throw new RangeError(`${name} \u5FC5\u987B\u662F\u975E\u8D1F\u5B89\u5168\u6574\u6570`);
    }
    return value;
  }
  function hlcFromDict(raw) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) {
      throw new RangeError("hlc \u5FC5\u987B\u662F\u5BF9\u8C61");
    }
    const record = raw;
    const physical = assertNonNegativeInt(record.physical_ms, "hlc.physical_ms");
    const logical = assertNonNegativeInt(record.logical, "hlc.logical");
    const device = record.device_id;
    if (typeof device !== "string" || !device) {
      throw new RangeError("hlc.device_id \u5FC5\u987B\u662F\u975E\u7A7A\u5B57\u7B26\u4E32");
    }
    return { physicalMs: physical, logical, deviceId: device };
  }
  var HybridLogicalClock = class {
    constructor(deviceId, options = {}) {
      __publicField(this, "physical");
      __publicField(this, "logical");
      __publicField(this, "clockMs");
      __publicField(this, "deviceId");
      if (!deviceId) throw new RangeError("HLC \u9700\u8981 device_id\uFF08\u5168\u5E8F\u51B3\u80DC\u4F4D\uFF0C\u4E0D\u5141\u8BB8\u7A7A\uFF09");
      this.deviceId = deviceId;
      this.clockMs = options.clockMs ?? (() => Date.now());
      this.physical = options.initial ? options.initial[0] : 0;
      this.logical = options.initial ? options.initial[1] : 0;
    }
    /** 当前 (physical_ms, logical)（持久化恢复用）。 */
    get state() {
      return [this.physical, this.logical];
    }
    /** 本地事件打戳（严格大于本实例此前发出的任何一枚）。 */
    now() {
      const pt = this.clockMs();
      if (pt > this.physical) {
        this.physical = pt;
        this.logical = 0;
      } else {
        this.logical += 1;
      }
      return { physicalMs: this.physical, logical: this.logical, deviceId: this.deviceId };
    }
    /** 观察远端时间戳后推进本地状态（保因果）：返回值 > remote 且 > 本实例此前任何状态。 */
    observe(remote) {
      const pt = this.clockMs();
      const physical = Math.max(this.physical, remote.physicalMs, pt);
      if (physical === this.physical && physical === remote.physicalMs) {
        this.logical = Math.max(this.logical, remote.logical) + 1;
      } else if (physical === this.physical) {
        this.logical = this.logical + 1;
      } else if (physical === remote.physicalMs) {
        this.logical = remote.logical + 1;
      } else {
        this.logical = 0;
      }
      this.physical = physical;
      return { physicalMs: this.physical, logical: this.logical, deviceId: this.deviceId };
    }
  };

  // src/sync/storage.ts
  function availableStorage() {
    try {
      const probe = "__lingxi_sync_probe__";
      window.localStorage.setItem(probe, "1");
      window.localStorage.removeItem(probe);
      return window.localStorage;
    } catch {
      return null;
    }
  }
  var LocalStorageKv = class {
    constructor(storage = availableStorage()) {
      __publicField(this, "storage");
      this.storage = storage;
    }
    get available() {
      return this.storage !== null;
    }
    get(key) {
      if (!this.storage) return null;
      try {
        return this.storage.getItem(key);
      } catch {
        return null;
      }
    }
    set(key, value) {
      if (!this.storage) return;
      try {
        this.storage.setItem(key, value);
      } catch (err) {
        console.warn("[lingxi-sync] kv set failed:", key, err);
      }
    }
    remove(key) {
      if (!this.storage) return;
      try {
        this.storage.removeItem(key);
      } catch {
      }
    }
    keysWithPrefix(prefix) {
      if (!this.storage) return [];
      const out = [];
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
  };
  var KEYS = {
    deviceId: "lingxi_sync.device_id",
    hlcState: "lingxi_sync.hlc_state",
    cursor: "lingxi_sync.cursor",
    assetPrefix: "lingxi_sync.asset.",
    appliedPrefix: "lingxi_sync.applied.",
    configPrefix: "lingxi_sync.config.",
    lastHeartbeat: "lingxi_sync.last_heartbeat"
  };

  // src/sync/configStore.ts
  var APPLIED_LEDGER_CAP = 2e3;
  var ConfigStore = class {
    constructor(kv) {
      __publicField(this, "kv", kv);
    }
    // ── 设备标识（跨配对保留：同一台设备）──────────────────────
    loadDeviceId() {
      return this.kv.get(KEYS.deviceId);
    }
    saveDeviceId(deviceId) {
      this.kv.set(KEYS.deviceId, deviceId);
    }
    // ── HLC 时钟状态（跨页面生命周期单调续接）──────────────────
    loadHlcState() {
      const raw = this.kv.get(KEYS.hlcState);
      if (!raw) return null;
      try {
        const value = JSON.parse(raw);
        const physical = value.physical_ms;
        const logical = value.logical;
        if (typeof physical === "number" && Number.isSafeInteger(physical) && physical >= 0 && typeof logical === "number" && Number.isSafeInteger(logical) && logical >= 0) {
          return [physical, logical];
        }
      } catch {
      }
      return null;
    }
    saveHlcState(physical, logical) {
      this.kv.set(KEYS.hlcState, JSON.stringify({ physical_ms: physical, logical }));
    }
    // ── 游标水位（null = 未自举）───────────────────────────────
    loadCursor() {
      const raw = this.kv.get(KEYS.cursor);
      if (raw === null) return null;
      const value = Number(raw);
      return Number.isSafeInteger(value) && value >= 0 ? value : null;
    }
    saveCursor(cursor) {
      this.kv.set(KEYS.cursor, String(cursor));
    }
    // ── 资产裁决账本（HLC-LWW 依据）────────────────────────────
    loadAsset(assetKey) {
      const raw = this.kv.get(KEYS.assetPrefix + assetKey);
      if (!raw) return null;
      try {
        const value = JSON.parse(raw);
        const hlc = hlcFromDict(value.hlc);
        const version = value.version;
        const ulid = value.ulid;
        const contentHash = value.content_hash;
        if (typeof version === "number" && typeof ulid === "string" && typeof contentHash === "string") {
          return { hlc, version, ulid, contentHash };
        }
      } catch {
      }
      return null;
    }
    saveAsset(assetKey, record) {
      this.kv.set(
        KEYS.assetPrefix + assetKey,
        JSON.stringify({
          hlc: {
            physical_ms: record.hlc.physicalMs,
            logical: record.hlc.logical,
            device_id: record.hlc.deviceId
          },
          version: record.version,
          ulid: record.ulid,
          content_hash: record.contentHash
        })
      );
    }
    assetCount() {
      return this.kv.keysWithPrefix(KEYS.assetPrefix).length;
    }
    // ── 已应用账本（ULID 幂等）─────────────────────────────────
    appliedOutcome(ulid) {
      return this.kv.get(KEYS.appliedPrefix + ulid);
    }
    markApplied(ulid, outcome, appliedAtIso) {
      this.kv.set(KEYS.appliedPrefix + ulid, JSON.stringify({ outcome, applied_at: appliedAtIso }));
      this.trimAppliedLedger();
    }
    appliedCount() {
      return this.kv.keysWithPrefix(KEYS.appliedPrefix).length;
    }
    trimAppliedLedger() {
      const keys = this.kv.keysWithPrefix(KEYS.appliedPrefix).sort();
      const excess = keys.length - APPLIED_LEDGER_CAP;
      for (let i = 0; i < excess; i++) this.kv.remove(keys[i]);
    }
    // ── 应用内容（app 侧配置镜像面）────────────────────────────
    loadConfigContent(assetId) {
      return this.kv.get(KEYS.configPrefix + assetId);
    }
    saveConfigContent(assetId, content) {
      this.kv.set(KEYS.configPrefix + assetId, content);
    }
    appliedConfigIds() {
      return this.kv.keysWithPrefix(KEYS.configPrefix).map((key) => key.slice(KEYS.configPrefix.length)).sort();
    }
    // ── 配对状态作废（join 成功/unpair 同款；HLC 与 device_id 保留）──
    resetPairingState() {
      this.kv.remove(KEYS.cursor);
      this.kv.remove(KEYS.lastHeartbeat);
      for (const key of this.kv.keysWithPrefix(KEYS.assetPrefix)) this.kv.remove(key);
      for (const key of this.kv.keysWithPrefix(KEYS.appliedPrefix)) this.kv.remove(key);
      for (const key of this.kv.keysWithPrefix(KEYS.configPrefix)) this.kv.remove(key);
    }
    // ── 心跳痕迹（面板展示）────────────────────────────────────
    loadLastHeartbeat() {
      return this.kv.get(KEYS.lastHeartbeat);
    }
    saveLastHeartbeat(atIso) {
      this.kv.set(KEYS.lastHeartbeat, atIso);
    }
    /** 单资产现值 HLC（面板/诊断便捷面；无记录 = null）。 */
    assetHlc(assetKey) {
      return this.loadAsset(assetKey)?.hlc ?? null;
    }
    /** LWW 单步裁决可见面（测试与诊断）：event 是否胜过本地现值。 */
    isEventStale(assetKey, eventHlc) {
      const local = this.loadAsset(assetKey);
      return local !== null && compareHlc(eventHlc, local.hlc) <= 0;
    }
  };

  // src/sync/panel.ts
  var SyncPanel = class {
    constructor(deps) {
      __publicField(this, "engine");
      __publicField(this, "credentialBackend");
      __publicField(this, "host", null);
      __publicField(this, "panel", null);
      __publicField(this, "chip", null);
      this.engine = deps.engine;
      this.credentialBackend = deps.credentialBackend;
    }
    /** 挂载浮动入口（幂等：已挂载先卸）。 */
    mount(root = document.body) {
      this.unmount();
      const host = document.createElement("div");
      host.id = "lingxi-sync-panel-host";
      const chip = document.createElement("button");
      chip.type = "button";
      chip.id = "lingxi-sync-chip";
      chip.setAttribute("data-testid", "sync-shell-chip");
      chip.setAttribute("aria-label", "\u540C\u6B65\u8BBE\u7F6E");
      chip.textContent = "\u27F3";
      chip.addEventListener("click", () => this.toggle());
      host.appendChild(chip);
      root.appendChild(host);
      this.host = host;
      this.chip = chip;
    }
    unmount() {
      this.close();
      this.host?.remove();
      this.host = null;
      this.chip = null;
    }
    toggle() {
      if (this.panel) this.close();
      else void this.open();
    }
    async open() {
      this.close();
      const host = this.host ?? document.body;
      const panel = document.createElement("div");
      panel.id = "lingxi-sync-panel";
      panel.setAttribute("data-testid", "sync-shell-panel");
      const shadow = panel.attachShadow({ mode: "open" });
      shadow.innerHTML = TEMPLATE;
      host.appendChild(panel);
      this.panel = panel;
      this.requireEl(shadow, "sync-now-button").addEventListener(
        "click",
        () => void this.handleSyncNow()
      );
      this.requireEl(shadow, "unpair-button").addEventListener(
        "click",
        () => void this.handleUnpair()
      );
      this.requireEl(shadow, "panel-close-button").addEventListener(
        "click",
        () => this.close()
      );
      this.refresh();
    }
    close() {
      this.panel?.remove();
      this.panel = null;
    }
    /** 现读引擎状态重绘（打开时与每个动作完成后调用）。 */
    refresh() {
      const shadow = this.panel?.shadowRoot;
      if (!shadow) return;
      const status = this.engine.status();
      this.requireEl(shadow, "sync-shell-status").textContent = describeStatus(status);
      this.requireEl(shadow, "sync-shell-detail").textContent = describeDetail(status, this.credentialBackend());
      this.requireEl(shadow, "paired-actions").style.display = status.paired ? "" : "none";
    }
    async handleSyncNow() {
      const shadow = this.requireShadow();
      const result = this.requireEl(shadow, "sync-now-result");
      result.textContent = "\u540C\u6B65\u4E2D\u2026";
      try {
        await this.engine.syncNow();
        result.textContent = "\u5DF2\u5B8C\u6210\u672C\u8F6E\u540C\u6B65";
      } catch (err) {
        result.textContent = err instanceof Error ? err.message : String(err);
      }
      this.refresh();
    }
    async handleUnpair() {
      const shadow = this.requireShadow();
      if (!window.confirm("\u89E3\u9664\u914D\u5BF9\u5C06\u6E05\u9664\u672C\u673A\u51ED\u636E\u4E0E\u540C\u6B65\u8FDB\u5EA6\uFF0C\u786E\u8BA4\uFF1F")) return;
      await this.engine.unpair();
      this.refresh();
    }
    requireShadow() {
      if (!this.panel?.shadowRoot) throw new Error("sync panel not open");
      return this.panel.shadowRoot;
    }
    requireEl(root, testId) {
      const el = root.querySelector(`[data-testid="${testId}"]`);
      if (!el) throw new Error(`sync panel element missing: ${testId}`);
      return el;
    }
  };
  function describeStatus(status) {
    if (!status.paired) {
      return "\u672A\u914D\u5BF9\uFF1A\u767B\u5F55\u540E\u81EA\u52A8\u5165\u7F51\uFF08admin \u767B\u5F55\u5373\u5B8C\u6210\u8BBE\u5907\u5165\u7F51\uFF09";
    }
    return "\u5DF2\u914D\u5BF9" + (status.bootstrapDone ? "\uFF08\u5DF2\u81EA\u4E3E\uFF09" : "\uFF08\u81EA\u4E3E\u5F85\u5B8C\u6210\uFF1Arelay \u4E0D\u53EF\u8FBE\u65F6\u81EA\u52A8\u91CD\u8BD5\uFF09");
  }
  function describeDetail(status, credentialBackend) {
    const parts = [];
    if (status.paired) {
      parts.push(`\u8BBE\u5907 ${status.deviceId ?? "?"}`, `\u6863\u4F4D ${status.tier}`);
      if (status.relayUrl) parts.push(`relay ${status.relayUrl}`);
      parts.push(`\u6E38\u6807 ${status.cursor ?? "-"}`);
      parts.push(`\u6700\u540E\u5FC3\u8DF3 ${status.lastHeartbeatAt ?? "\u65E0"}`);
    }
    if (status.lastError) parts.push(`\u6700\u8FD1\u9519\u8BEF ${status.lastError}`);
    parts.push(`\u51ED\u636E\u901A\u9053 ${credentialBackend}`);
    return parts.join(" \xB7 ");
  }
  var TEMPLATE = `
<style>
  :host {
    position: fixed;
    right: 12px;
    bottom: calc(64px + var(--safe-inset-bottom, 0px));
    z-index: 2147483000;
    font-family: system-ui, sans-serif;
  }
  .card {
    width: min(320px, calc(100vw - 24px));
    max-height: 70vh;
    overflow: auto;
    background: #1f2430;
    color: #e7eaf0;
    border: 1px solid #3a4254;
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
    padding: 14px;
    font-size: 12px;
    line-height: 1.6;
  }
  h2 { margin: 0 0 8px; font-size: 14px; }
  p { margin: 4px 0; word-break: break-all; }
  .row { display: flex; gap: 8px; margin-top: 8px; flex-wrap: wrap; }
  button.act {
    flex: 1;
    min-height: 30px;
    border: 1px solid #3a4254;
    border-radius: 8px;
    background: #2c3547;
    color: #e7eaf0;
    font-size: 12px;
    cursor: pointer;
  }
  button.act:disabled { opacity: 0.5; cursor: default; }
  button.primary { background: #22d3ee; border-color: #22d3ee; color: #08222a; font-weight: 600; }
  button.close {
    float: right;
    border: none;
    background: transparent;
    color: #8b93a5;
    font-size: 14px;
    cursor: pointer;
  }
  .hint { color: #9aa3b5; }
  .result-ok { color: #4ade80; }
  .result-err { color: #f87171; }
</style>
<div class="card" part="card">
  <button type="button" class="close" data-testid="panel-close-button" aria-label="\u5173\u95ED\u540C\u6B65\u8BBE\u7F6E">\xD7</button>
  <h2>\u591A\u7AEF\u540C\u6B65\uFF08\u624B\u673A\u7AEF\uFF09</h2>
  <p data-testid="sync-shell-status"></p>
  <p class="hint" data-testid="sync-shell-detail"></p>
  <div data-testid="paired-actions">
    <div class="row">
      <button type="button" class="act primary" data-testid="sync-now-button">\u7ACB\u5373\u540C\u6B65</button>
      <button type="button" class="act" data-testid="unpair-button">\u89E3\u9664\u914D\u5BF9</button>
    </div>
    <p data-testid="sync-now-result"></p>
  </div>
  <p class="hint">\u540C\u6B65\u8303\u56F4\uFF1Aconfig \u914D\u7F6E\u9762\u4E0B\u884C\u5E94\u7528\uFF08HLC-LWW \u540E\u6539\u8D62\uFF09+ 30s \u5FC3\u8DF3\uFF1B\u672C\u673A\u53D8\u66F4\u4E0A\u884C\u6355\u83B7\u5728\u540E\u7EED\u7248\u672C\u63D0\u4F9B\u3002\u5165\u7F51\u7ECF\u767B\u5F55\u81EA\u52A8\u5B8C\u6210\uFF0C\u65E0\u9700\u8F93\u7801\u3002</p>
</div>
`;
  function injectChipStyle(root) {
    if (root.querySelector("style[data-lingxi-sync-chip]")) return;
    const style = document.createElement("style");
    style.setAttribute("data-lingxi-sync-chip", "");
    style.textContent = `
    #lingxi-sync-panel-host { position: static; }
    #lingxi-sync-chip {
      position: fixed;
      right: 12px;
      bottom: calc(12px + var(--safe-inset-bottom, 0px));
      z-index: 2147483000;
      width: 40px;
      height: 40px;
      border-radius: 50%;
      border: 1px solid rgba(128, 140, 170, 0.55);
      background: rgba(31, 36, 48, 0.82);
      color: #e7eaf0;
      font-size: 18px;
      line-height: 1;
      cursor: pointer;
      opacity: 0.72;
    }
    #lingxi-sync-chip:active { opacity: 1; }
  `;
    root.appendChild(style);
  }

  // src/sync/relayClient.ts
  var RelayError = class extends Error {
    constructor(status, message) {
      super(message);
      __publicField(this, "status");
      this.name = "RelayError";
      this.status = status;
    }
  };
  var REQUEST_TIMEOUT_MS = 1e4;
  var RelayClient = class {
    constructor(fetchImpl = (...args) => fetch(...args)) {
      __publicField(this, "fetchImpl", fetchImpl);
    }
    /** admin JWT 直签 per-device token（POST {relay}/enroll，Bearer JWT 即凭据）。 */
    async enrollWithJwt(relayUrl, jwt, request) {
      const body = await this.postJson(relayUrl, joinPath(relayUrl, "/enroll"), jwt, {
        device_id: request.deviceId,
        tier: request.tier,
        app_version: request.appVersion,
        capabilities: request.capabilities
      });
      return {
        deviceId: requireString(body, "device_id", "enroll"),
        deviceToken: requireString(body, "device_token", "enroll"),
        tenantId: typeof body.tenant_id === "string" ? body.tenant_id : "",
        // 响应无 relay_url 回显：调用 URL 即事实地址。
        relayUrl
      };
    }
    /** 设备注册（POST {relay}/devices；幂等，自举首步）。 */
    async register(relayUrl, token, registration) {
      await this.postJson(relayUrl, joinPath(relayUrl, "/devices"), token, {
        device_id: registration.deviceId,
        tier: registration.tier,
        app_version: registration.appVersion,
        capabilities: registration.capabilities
      });
    }
    /** 心跳上报（POST {relay}/devices/heartbeat）。 */
    async heartbeat(relayUrl, token, registration) {
      const body = await this.postJson(relayUrl, joinPath(relayUrl, "/devices/heartbeat"), token, {
        device_id: registration.deviceId,
        tier: registration.tier,
        app_version: registration.appVersion
      });
      return typeof body.heartbeat_at === "string" ? body.heartbeat_at : "";
    }
    /** cursor 增量拉取（GET {relay}/events?since_cursor=，严格大于、按 cursor 升序）。 */
    async pullEvents(relayUrl, token, sinceCursor, limit) {
      const query = new URLSearchParams({ since_cursor: String(sinceCursor) });
      if (limit !== void 0) query.set("limit", String(limit));
      const body = await this.getJson(relayUrl, `${joinPath(relayUrl, "/events")}?${query}`, token);
      const events = Array.isArray(body.events) ? body.events : [];
      return { events, count: events.length };
    }
    /** 入网基线（GET {relay}/bootstrap，层级权威①：relay 按端位阶裁定各资产基线）。 */
    async bootstrap(relayUrl, token) {
      const body = await this.getJson(relayUrl, joinPath(relayUrl, "/bootstrap"), token);
      const events = Array.isArray(body.events) ? body.events : [];
      const cursor = body.cursor;
      return {
        device: typeof body.device === "string" ? body.device : "",
        events,
        cursor: typeof cursor === "number" && Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : 0
      };
    }
    async getJson(relayUrl, url, token) {
      const headers = {};
      if (token) headers.Authorization = `Bearer ${token}`;
      return this.request(url, { method: "GET", headers }, relayUrl);
    }
    async postJson(relayUrl, url, token, payload) {
      const headers = { "Content-Type": "application/json" };
      if (token) headers.Authorization = `Bearer ${token}`;
      return this.request(
        url,
        { method: "POST", headers, body: JSON.stringify(payload) },
        relayUrl
      );
    }
    async request(url, init, relayUrl) {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
      let response;
      try {
        response = await this.fetchImpl(url, { ...init, signal: controller.signal });
      } catch (err) {
        throw new RelayError(0, `relay \u4E0D\u53EF\u8FBE\uFF08${describeUrl(relayUrl)}\uFF09: ${errorMessage(err)}`);
      } finally {
        clearTimeout(timer);
      }
      const text = await response.text();
      let body = null;
      try {
        body = text ? JSON.parse(text) : null;
      } catch {
        throw new RelayError(response.status, `relay \u54CD\u5E94\u4E0D\u662F JSON\uFF08HTTP ${response.status}\uFF09`);
      }
      if (!response.ok) {
        throw new RelayError(response.status, extractErrorMessage(body, response.status));
      }
      if (body === null || typeof body !== "object" || Array.isArray(body)) {
        throw new RelayError(response.status, "relay \u54CD\u5E94\u5FC5\u987B\u662F JSON \u5BF9\u8C61");
      }
      return body;
    }
  };
  function joinPath(relayUrl, path) {
    return relayUrl.replace(/\/+$/, "") + path;
  }
  function requireString(body, key, where) {
    const value = body[key];
    if (typeof value !== "string" || !value) {
      throw new RelayError(502, `relay ${where} \u54CD\u5E94\u7F3A ${key}`);
    }
    return value;
  }
  function extractErrorMessage(body, status) {
    const inner = body !== null && typeof body === "object" && !Array.isArray(body) ? body.error : null;
    if (inner !== null && typeof inner === "object" && !Array.isArray(inner)) {
      const message = inner.message;
      if (typeof message === "string" && message) return message;
    }
    if (typeof inner === "string" && inner) return inner;
    return `relay \u9519\u8BEF\uFF08HTTP ${status}\uFF09`;
  }
  function describeUrl(relayUrl) {
    try {
      return new URL(relayUrl).host;
    } catch {
      return relayUrl;
    }
  }
  function errorMessage(err) {
    return err instanceof Error ? err.message : String(err);
  }

  // src/sync/ulid.ts
  var ENCODING = "0123456789ABCDEFGHJKMNPQRSTVWXYZ";
  var ULID_LENGTH = 26;
  var RANDOM_BITS = 80;
  var MAX_RANDOM = 2n ** BigInt(RANDOM_BITS) - 1n;
  var ULID_CHARSET = ENCODING;

  // src/sync/syncEvent.ts
  var REQUIRED_FIELDS = ["ulid", "device_id", "asset_type", "version", "updated_at"];
  function assetKeyOf(assetType, assetId) {
    return assetId ? `${assetType}/${assetId}` : assetType;
  }
  function eventAssetKey(event) {
    return assetKeyOf(event.assetType, event.assetId);
  }
  function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
  }
  function validateEvent(raw) {
    if (!isPlainObject(raw)) throw new RangeError("sync_event \u5FC5\u987B\u662F\u5BF9\u8C61");
    const missing = REQUIRED_FIELDS.filter((name) => raw[name] === null || raw[name] === void 0);
    if (missing.length > 0) {
      throw new RangeError(`sync_event \u7F3A\u5C11\u5FC5\u586B\u5B57\u6BB5: ${missing.join(", ")}`);
    }
    const ulidValue = raw.ulid;
    if (typeof ulidValue !== "string" || ulidValue.length !== ULID_LENGTH || [...ulidValue].some((ch) => !ULID_CHARSET.includes(ch))) {
      throw new RangeError("ulid \u5FC5\u987B\u662F 26 \u4F4D\u5927\u5199 Crockford Base32");
    }
    const deviceId = raw.device_id;
    if (typeof deviceId !== "string" || !deviceId) {
      throw new RangeError("device_id \u5FC5\u987B\u662F\u975E\u7A7A\u5B57\u7B26\u4E32");
    }
    const assetType = raw.asset_type;
    if (typeof assetType !== "string" || !assetType) {
      throw new RangeError("asset_type \u5FC5\u987B\u662F\u975E\u7A7A\u5B57\u7B26\u4E32");
    }
    const version = raw.version;
    if (typeof version === "boolean" || typeof version !== "number" || !Number.isSafeInteger(version) || version < 0) {
      throw new RangeError("version \u5FC5\u987B\u662F\u975E\u8D1F\u6574\u6570");
    }
    const updatedAt = raw.updated_at;
    if (typeof updatedAt !== "string" || !updatedAt) {
      throw new RangeError("updated_at \u5FC5\u987B\u662F\u975E\u7A7A\u5B57\u7B26\u4E32");
    }
    const assetId = raw.asset_id === void 0 ? "" : raw.asset_id;
    if (typeof assetId !== "string") throw new RangeError("asset_id \u5FC5\u987B\u662F\u5B57\u7B26\u4E32");
    const payload = raw.payload === void 0 ? {} : raw.payload;
    if (!isPlainObject(payload)) throw new RangeError("payload \u5FC5\u987B\u662F\u5BF9\u8C61");
    const rawHlc = raw.hlc;
    if (rawHlc !== null && rawHlc !== void 0 && !isPlainObject(rawHlc)) {
      throw new RangeError("hlc \u5FC5\u987B\u662F\u5BF9\u8C61");
    }
    const hlc = rawHlc === null || rawHlc === void 0 ? null : hlcFromDict(rawHlc);
    const rawCursor = raw.cursor === void 0 ? 0 : raw.cursor;
    if (typeof rawCursor === "boolean" || typeof rawCursor !== "number" || !Number.isSafeInteger(rawCursor) || rawCursor < 0) {
      throw new RangeError("cursor \u5FC5\u987B\u662F\u975E\u8D1F\u6574\u6570");
    }
    return {
      ulid: ulidValue,
      deviceId,
      assetType,
      assetId,
      version,
      updatedAt,
      payload,
      hlc,
      cursor: rawCursor
    };
  }

  // src/sync/syncEngine.ts
  var SYNC_ASSET_TYPE = "config";
  var DEVICE_TIER = "phone";
  var TICK_INTERVAL_MS = 3e4;
  async function sha256Hex(text) {
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
    return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
  }
  var SyncEngine = class {
    constructor(deps) {
      __publicField(this, "credential");
      __publicField(this, "store");
      __publicField(this, "client");
      __publicField(this, "clockMs");
      __publicField(this, "nowIso");
      __publicField(this, "schedule");
      __publicField(this, "appVersion");
      __publicField(this, "joinRelayUrl");
      __publicField(this, "clock", null);
      __publicField(this, "cancelSchedule", null);
      __publicField(this, "running", false);
      __publicField(this, "ticking", false);
      __publicField(this, "active", null);
      __publicField(this, "bootstrapDone", false);
      __publicField(this, "pulledTotal", 0);
      __publicField(this, "appliedTotal", 0);
      __publicField(this, "staleTotal", 0);
      __publicField(this, "heartbeatsTotal", 0);
      __publicField(this, "lastError", "");
      this.credential = deps.credential;
      this.store = deps.store;
      this.client = deps.client;
      this.clockMs = deps.clockMs ?? (() => Date.now());
      this.nowIso = deps.nowIso ?? (() => (/* @__PURE__ */ new Date()).toISOString());
      this.schedule = deps.schedule ?? defaultSchedule;
      this.appVersion = deps.appVersion ?? shellAppVersion();
      this.joinRelayUrl = deps.joinRelayUrl;
    }
    // ── 生命周期 ───────────────────────────────────────────────
    /** 已配对则续跑（页面加载入口）；返回是否进入运行态。 */
    async start() {
      const active = await this.credential.load();
      if (!active) return false;
      this.activate(active);
      this.running = true;
      this.cancelSchedule = this.schedule(() => void this.runTick(), TICK_INTERVAL_MS);
      await this.tryBootstrapIfNeeded();
      return true;
    }
    stop() {
      this.running = false;
      if (this.cancelSchedule) {
        this.cancelSchedule();
        this.cancelSchedule = null;
      }
    }
    /** 登录入网：admin JWT 直签 → 落凭据 → 作废旧配对状态 → 起轮（bootstrap 失败
     * 不回滚，下拍重试）。JWT 端侧整串透传不解释（relay 内核 admin 执法签发，
     * ADR 2026-11-02-login-as-enrollment）；relayUrlOverride 供前端按当前连接
     * 基址显式指定（缺省走装配层解析链）。 */
    async joinWithAuth(jwt, relayUrlOverride) {
      const trimmed = jwt.trim();
      if (!trimmed) throw new RelayError(400, "\u5165\u7F51\u51ED\u636E\u4E3A\u7A7A");
      const deviceId = this.ensureDeviceId();
      const relayUrl = relayUrlOverride ?? this.joinRelayUrl();
      const outcome = await this.client.enrollWithJwt(relayUrl, trimmed, {
        deviceId,
        tier: DEVICE_TIER,
        appVersion: this.appVersion,
        capabilities: { asset_types: [SYNC_ASSET_TYPE] }
      });
      const active = {
        relayUrl: outcome.relayUrl || relayUrl,
        // 本机 device_id 为准（relay 签发契约回显请求值；防顶名一致性同源）。
        deviceId,
        deviceToken: outcome.deviceToken,
        tenantId: outcome.tenantId,
        tier: DEVICE_TIER,
        appVersion: this.appVersion,
        pairedAt: this.nowIso()
      };
      this.store.resetPairingState();
      await this.credential.save(active);
      this.activate(active);
      this.running = true;
      if (!this.cancelSchedule) {
        this.cancelSchedule = this.schedule(() => void this.runTick(), TICK_INTERVAL_MS);
      }
      await this.tryBootstrapIfNeeded();
      return active;
    }
    /** 解除配对：凭据与配对状态同清（HLC 时钟与设备 UUID 保留）；返回后引擎停转。 */
    async unpair() {
      this.stop();
      this.store.resetPairingState();
      await this.credential.clear();
      this.active = null;
      this.bootstrapDone = false;
      this.lastError = "";
    }
    /** 手动立即同步（面板「立即同步」）。 */
    async syncNow() {
      if (!this.active) throw new RelayError(400, "\u5C1A\u672A\u914D\u5BF9");
      await this.runTick();
    }
    // ── 状态面（面板展示）─────────────────────────────────────
    status() {
      return {
        paired: this.active !== null,
        deviceId: this.active?.deviceId ?? this.store.loadDeviceId(),
        relayUrl: this.active?.relayUrl ?? null,
        tier: this.active?.tier ?? DEVICE_TIER,
        appVersion: this.active?.appVersion ?? this.appVersion,
        bootstrapDone: this.bootstrapDone,
        cursor: this.store.loadCursor(),
        pulledTotal: this.pulledTotal,
        appliedTotal: this.appliedTotal,
        staleTotal: this.staleTotal,
        heartbeatsTotal: this.heartbeatsTotal,
        lastHeartbeatAt: this.store.loadLastHeartbeat(),
        lastError: this.lastError
      };
    }
    // ── 内部：自举 / 单拍 / 合并 ───────────────────────────────
    activate(active) {
      this.active = active;
      if (!this.clock) {
        const initial = this.store.loadHlcState();
        this.clock = new HybridLogicalClock(active.deviceId, {
          clockMs: this.clockMs,
          initial: initial ?? void 0
        });
      }
    }
    ensureDeviceId() {
      const existing = this.store.loadDeviceId();
      if (existing) return existing;
      const generated = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `phone-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
      this.store.saveDeviceId(generated);
      return generated;
    }
    needsBootstrap() {
      return this.store.loadCursor() === null && this.store.assetCount() === 0;
    }
    async tryBootstrapIfNeeded() {
      if (!this.active || !this.needsBootstrap()) return;
      try {
        await this.bootstrap();
      } catch (err) {
        this.lastError = `bootstrap: ${errorMessage2(err)}`;
      }
    }
    async bootstrap() {
      const active = this.active;
      if (!active) throw new RelayError(400, "\u5C1A\u672A\u914D\u5BF9");
      await this.client.register(active.relayUrl, active.deviceToken, {
        deviceId: active.deviceId,
        tier: active.tier,
        appVersion: active.appVersion,
        capabilities: { asset_types: [SYNC_ASSET_TYPE] }
      });
      const page = await this.client.bootstrap(active.relayUrl, active.deviceToken);
      let applied = 0;
      for (const wire of page.events) {
        if (await this.ingestWire(wire) === "applied") applied += 1;
      }
      this.store.saveCursor(page.cursor);
      this.bootstrapDone = true;
      this.lastError = "";
      console.info(
        `[lingxi-sync] \u5165\u7F51\u81EA\u4E3E\u5B8C\u6210: device=${active.deviceId} baseline_applied=${applied} cursor=${page.cursor}`
      );
    }
    async runTick() {
      if (!this.running || !this.active || this.ticking) return;
      this.ticking = true;
      try {
        if (this.needsBootstrap()) {
          await this.bootstrap();
        }
        await this.pullOnce();
      } catch (err) {
        this.lastError = errorMessage2(err);
      }
      try {
        await this.heartbeatOnce();
      } finally {
        this.ticking = false;
      }
    }
    async pullOnce() {
      const active = this.active;
      if (!active) return;
      const watermark = this.store.loadCursor();
      if (watermark === null) return;
      const page = await this.client.pullEvents(active.relayUrl, active.deviceToken, watermark);
      let highest = watermark;
      for (const wire of page.events) {
        await this.ingestWire(wire);
        const cursor = readCursor(wire);
        if (cursor > highest) highest = cursor;
      }
      if (highest !== watermark) this.store.saveCursor(highest);
    }
    /** 下行单事件：校验 → 回显/幂等跳过 → observe（保因果）→ HLC-LWW 合并。 */
    async ingestWire(wire) {
      let event;
      try {
        event = validateEvent(wire);
      } catch (err) {
        console.warn("[lingxi-sync] \u4E0B\u884C\u4E8B\u4EF6 schema \u8FDD\u7EA6\u4E22\u5F03:", errorMessage2(err));
        return "invalid";
      }
      const active = this.active;
      if (!active) return "invalid";
      if (event.deviceId === active.deviceId) {
        this.pulledTotal += 1;
        return "self";
      }
      if (this.store.appliedOutcome(event.ulid) !== null) {
        this.pulledTotal += 1;
        return "duplicate";
      }
      if (event.hlc !== null) {
        this.observe(event.hlc);
      }
      const outcome = await this.merge(event);
      this.store.markApplied(event.ulid, outcome, this.nowIso());
      this.pulledTotal += 1;
      if (outcome === "applied") this.appliedTotal += 1;
      if (outcome === "stale") this.staleTotal += 1;
      return outcome;
    }
    observe(remote) {
      if (!this.clock) return;
      this.clock.observe(remote);
      const [physical, logical] = this.clock.state;
      this.store.saveHlcState(physical, logical);
    }
    async merge(event) {
      if (event.assetType !== SYNC_ASSET_TYPE) return "unsupported_type";
      if (event.hlc === null) return "no_hlc";
      const key = eventAssetKey(event);
      const local = this.store.loadAsset(key);
      if (local !== null && compareHlc(event.hlc, local.hlc) <= 0) return "stale";
      const content = event.payload.content;
      if (typeof content !== "string") return "no_content";
      if (!event.assetId) return "no_asset_id";
      this.store.saveConfigContent(event.assetId, content);
      this.store.saveAsset(key, {
        hlc: event.hlc,
        version: event.version,
        ulid: event.ulid,
        contentHash: await sha256Hex(content)
      });
      return "applied";
    }
    async heartbeatOnce() {
      const active = this.active;
      if (!active) return;
      try {
        const at = await this.client.heartbeat(active.relayUrl, active.deviceToken, {
          deviceId: active.deviceId,
          tier: active.tier,
          appVersion: active.appVersion
        });
        this.store.saveLastHeartbeat(at || this.nowIso());
        this.heartbeatsTotal += 1;
      } catch (err) {
        this.lastError = `heartbeat: ${errorMessage2(err)}`;
      }
    }
  };
  function defaultSchedule(tick, intervalMs) {
    const handle = setInterval(tick, intervalMs);
    return () => clearInterval(handle);
  }
  function readCursor(wire) {
    const cursor = wire?.cursor;
    return typeof cursor === "number" && Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : 0;
  }
  function shellAppVersion() {
    const config = globalThis.LINGXI_SHELL_CONFIG;
    return typeof config?.versionName === "string" ? config.versionName : "unknown";
  }
  function errorMessage2(err) {
    return err instanceof Error ? err.message : String(err);
  }

  // src/sync/index.ts
  function resolveApiBase() {
    try {
      const userSetting = window.localStorage.getItem("server_base_url");
      if (userSetting && userSetting.trim()) return userSetting.trim();
    } catch {
    }
    const injected = globalThis.LINGXI_SHELL_CONFIG;
    if (typeof injected?.apiBase === "string" && injected.apiBase) return injected.apiBase;
    return "";
  }
  function relayBaseOf(apiBase) {
    if (!apiBase) return "/ext/sync_relay";
    return apiBase.replace(/\/+$/, "") + "/ext/sync_relay";
  }
  function boot() {
    if (globalThis.__LINGXI_SHELL_SYNC__) {
      return globalThis.__LINGXI_SYNC_API__ ?? null;
    }
    globalThis.__LINGXI_SHELL_SYNC__ = true;
    const kv = new LocalStorageKv();
    if (!kv.available) {
      console.warn("[lingxi-sync] localStorage \u4E0D\u53EF\u7528\uFF0C\u540C\u6B65\u6A21\u5757\u505C\u7528\uFF08\u72B6\u6001\u9762\u65E0\u6CD5\u6301\u4E45\u5316\uFF09");
      return null;
    }
    const credential = new CredentialStore(kv);
    const store = new ConfigStore(kv);
    const client = new RelayClient();
    const engine = new SyncEngine({
      credential,
      store,
      client,
      joinRelayUrl: () => relayBaseOf(resolveApiBase())
    });
    const panel = new SyncPanel({ engine, credentialBackend: () => credential.backend });
    void engine.start().then((started) => {
      if (started) console.info("[lingxi-sync] \u5DF2\u914D\u5BF9\uFF0C\u540C\u6B65\u5F15\u64CE\u7EED\u8DD1");
    }).catch((err) => console.warn("[lingxi-sync] \u5F15\u64CE\u542F\u52A8\u5931\u8D25:", err));
    if (typeof document !== "undefined" && document.body) {
      injectChipStyle(document.body);
      panel.mount(document.body);
    } else {
      console.warn("[lingxi-sync] document \u672A\u5C31\u7EEA\uFF0C\u4EC5\u66B4\u9732 window.LingxiSync \u7F16\u7A0B\u5165\u53E3");
    }
    const api = {
      engine,
      panel,
      joinWithAuth: (jwt, apiBase) => {
        const base = apiBase?.trim();
        return base ? engine.joinWithAuth(jwt, relayBaseOf(base)) : engine.joinWithAuth(jwt);
      },
      open: () => void panel.open(),
      close: () => panel.close()
    };
    globalThis.__LINGXI_SYNC_API__ = api;
    return api;
  }
  var lingxiSync = typeof window !== "undefined" ? boot() : null;
  if (typeof window !== "undefined") {
    window.LingxiSync = lingxiSync ?? void 0;
  }
})();
