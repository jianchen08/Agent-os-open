/**
 * 手机端同步引擎（最小闭环：下行应用 + 心跳上行）—— 与 sync_agent 引擎同构。
 *
 * 单拍节奏（缺省 30s，schedule 可注入）：needsBootstrap ? bootstrap : pull →
 * heartbeat。bootstrap = 注册 → GET /bootstrap（层级权威①，relay 裁定基线）→
 * 逐事件 HLC-LWW 应用 → 游标定位账本头；pull 自游标增量推进；心跳 30s 上报
 * （relay 在链判定数据源，窗口 90s）。
 *
 * 入网（ADR 2026-11-02-login-as-enrollment）：joinWithAuth 以 admin JWT 直签
 * per-device token（POST /enroll，Bearer JWT 即凭据，唯一认证动作 = 登录）。
 *
 * 合并语义（_merge 同构 + 一处端侧收紧）：仅 config 面；HLC-LWW 后改赢
 * （compareHlc ≤ 0 即 stale，端位阶不参与数据面裁决——ADR D2 意图定裁决）；
 * ULID 幂等（已应用重放跳过）；本端事件回显跳过。收紧点：无 HLC 事件直接
 * 拒收（no_hlc，不落盘）——relay 契约（无 HLC 不入账）保证此形态不应出现，
 * 端侧 fail-closed 兜底而非以本地钟补戳应用（裸墙钟 LWW 已被 ADR 否）。
 *
 * 半成品边界（台账如实记）：本地变更捕获上行（capture→outbox→flush）本刀不做，
 * capabilities 仍声明 config 面（下行应用亦属该资产面消费端）。
 */

import { CredentialStore, type SyncCredential } from "./credential";
import { ConfigStore } from "./configStore";
import { compareHlc, HybridLogicalClock, type HlcTimestamp } from "./hlc";
import type {
  BootstrapPage,
  DeviceRegistration,
  EnrollOutcome,
  EnrollRequest,
  PullPage,
  RelayClient,
} from "./relayClient";
import { RelayError } from "./relayClient";
import { eventAssetKey, validateEvent, type SyncEvent } from "./syncEvent";

export const SYNC_ASSET_TYPE = "config";
export const DEVICE_TIER = "phone";
export const TICK_INTERVAL_MS = 30_000;

export interface EngineStatus {
  paired: boolean
  deviceId: string | null
  relayUrl: string | null
  tier: string
  appVersion: string
  bootstrapDone: boolean
  cursor: number | null
  pulledTotal: number
  appliedTotal: number
  staleTotal: number
  heartbeatsTotal: number
  lastHeartbeatAt: string | null
  lastError: string
}

export interface SyncEngineDeps {
  credential: CredentialStore
  store: ConfigStore
  client: RelayTransport
  /** 兑换目标 relay 基址（装配层按壳基址解析链供给：用户设置 > 构建期注入）。 */
  joinRelayUrl: () => string
  clockMs?: () => number
  nowIso?: () => string
  /** 周期调度（测试注入替身断言节律）；返回取消函数。 */
  schedule?: (tick: () => void, intervalMs: number) => () => void
  appVersion?: string
}

export type MergeOutcome =
  | "applied"
  | "stale"
  | "self"
  | "duplicate"
  | "invalid"
  | "no_hlc"
  | "no_content"
  | "no_asset_id"
  | "unsupported_type";

/** relay 传输面（结构接口：RelayClient 即实现，测试可注入替身）。 */
export interface RelayTransport {
  enrollWithJwt(relayUrl: string, jwt: string, request: EnrollRequest): Promise<EnrollOutcome>
  register(relayUrl: string, token: string, registration: DeviceRegistration): Promise<void>
  heartbeat(
    relayUrl: string,
    token: string,
    registration: Omit<DeviceRegistration, "capabilities">,
  ): Promise<string>
  pullEvents(relayUrl: string, token: string, sinceCursor: number, limit?: number): Promise<PullPage>
  bootstrap(relayUrl: string, token: string): Promise<BootstrapPage>
}

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

export class SyncEngine {
  private readonly credential: CredentialStore;
  private readonly store: ConfigStore;
  private readonly client: RelayTransport;
  private readonly clockMs: () => number;
  private readonly nowIso: () => string;
  private readonly schedule: (tick: () => void, intervalMs: number) => () => void;
  private readonly appVersion: string;
  private readonly joinRelayUrl: () => string;

  private clock: HybridLogicalClock | null = null;
  private cancelSchedule: (() => void) | null = null;
  private running = false;
  private ticking = false;
  private active: SyncCredential | null = null;

  private bootstrapDone = false;
  private pulledTotal = 0;
  private appliedTotal = 0;
  private staleTotal = 0;
  private heartbeatsTotal = 0;
  private lastError = "";

  constructor(deps: SyncEngineDeps) {
    this.credential = deps.credential;
    this.store = deps.store;
    this.client = deps.client;
    this.clockMs = deps.clockMs ?? (() => Date.now());
    this.nowIso = deps.nowIso ?? (() => new Date().toISOString());
    this.schedule = deps.schedule ?? defaultSchedule;
    this.appVersion = deps.appVersion ?? shellAppVersion();
    this.joinRelayUrl = deps.joinRelayUrl;
  }

  // ── 生命周期 ───────────────────────────────────────────────

  /** 已配对则续跑（页面加载入口）；返回是否进入运行态。 */
  async start(): Promise<boolean> {
    const active = await this.credential.load();
    if (!active) return false;
    this.activate(active);
    this.running = true;
    this.cancelSchedule = this.schedule(() => void this.runTick(), TICK_INTERVAL_MS);
    await this.tryBootstrapIfNeeded();
    return true;
  }

  stop(): void {
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
  async joinWithAuth(jwt: string, relayUrlOverride?: string): Promise<SyncCredential> {
    const trimmed = jwt.trim();
    if (!trimmed) throw new RelayError(400, "入网凭据为空");
    const deviceId = this.ensureDeviceId();
    const relayUrl = relayUrlOverride ?? this.joinRelayUrl();
    const outcome = await this.client.enrollWithJwt(relayUrl, trimmed, {
      deviceId,
      tier: DEVICE_TIER,
      appVersion: this.appVersion,
      capabilities: { asset_types: [SYNC_ASSET_TYPE] },
    });
    const active: SyncCredential = {
      relayUrl: outcome.relayUrl || relayUrl,
      // 本机 device_id 为准（relay 签发契约回显请求值；防顶名一致性同源）。
      deviceId,
      deviceToken: outcome.deviceToken,
      tenantId: outcome.tenantId,
      tier: DEVICE_TIER,
      appVersion: this.appVersion,
      pairedAt: this.nowIso(),
    };
    // 先作废旧配对状态再落凭据：unpair→重入网或换号重配对都收敛到干净基线。
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
  async unpair(): Promise<void> {
    this.stop();
    this.store.resetPairingState();
    await this.credential.clear();
    this.active = null;
    this.bootstrapDone = false;
    this.lastError = "";
  }

  /** 手动立即同步（面板「立即同步」）。 */
  async syncNow(): Promise<void> {
    if (!this.active) throw new RelayError(400, "尚未配对");
    await this.runTick();
  }

  // ── 状态面（面板展示）─────────────────────────────────────

  status(): EngineStatus {
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
      lastError: this.lastError,
    };
  }

  // ── 内部：自举 / 单拍 / 合并 ───────────────────────────────

  private activate(active: SyncCredential): void {
    this.active = active;
    if (!this.clock) {
      const initial = this.store.loadHlcState();
      this.clock = new HybridLogicalClock(active.deviceId, {
        clockMs: this.clockMs,
        initial: initial ?? undefined,
      });
    }
  }

  private ensureDeviceId(): string {
    const existing = this.store.loadDeviceId();
    if (existing) return existing;
    const generated =
      typeof crypto.randomUUID === "function"
        ? crypto.randomUUID()
        : `phone-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
    this.store.saveDeviceId(generated);
    return generated;
  }

  private needsBootstrap(): boolean {
    return this.store.loadCursor() === null && this.store.assetCount() === 0;
  }

  private async tryBootstrapIfNeeded(): Promise<void> {
    if (!this.active || !this.needsBootstrap()) return;
    try {
      await this.bootstrap();
    } catch (err) {
      // relay 未可达：下拍重试（mirror python bootstrap 重试语义），不阻塞起轮。
      this.lastError = `bootstrap: ${errorMessage(err)}`;
    }
  }

  private async bootstrap(): Promise<void> {
    const active = this.active;
    if (!active) throw new RelayError(400, "尚未配对");
    await this.client.register(active.relayUrl, active.deviceToken, {
      deviceId: active.deviceId,
      tier: active.tier,
      appVersion: active.appVersion,
      capabilities: { asset_types: [SYNC_ASSET_TYPE] },
    });
    const page = await this.client.bootstrap(active.relayUrl, active.deviceToken);
    let applied = 0;
    for (const wire of page.events) {
      if ((await this.ingestWire(wire)) === "applied") applied += 1;
    }
    this.store.saveCursor(page.cursor);
    this.bootstrapDone = true;
    this.lastError = "";
    console.info(
      `[lingxi-sync] 入网自举完成: device=${active.deviceId} baseline_applied=${applied} cursor=${page.cursor}`,
    );
  }

  private async runTick(): Promise<void> {
    if (!this.running || !this.active || this.ticking) return;
    this.ticking = true;
    try {
      if (this.needsBootstrap()) {
        await this.bootstrap();
      }
      // 拉取与心跳错误隔离（mirror python：各自退避重试，互不挡拍）。
      await this.pullOnce();
    } catch (err) {
      this.lastError = errorMessage(err);
    }
    try {
      await this.heartbeatOnce();
    } finally {
      this.ticking = false;
    }
  }

  private async pullOnce(): Promise<void> {
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
  async ingestWire(wire: unknown): Promise<MergeOutcome> {
    let event: SyncEvent;
    try {
      event = validateEvent(wire);
    } catch (err) {
      console.warn("[lingxi-sync] 下行事件 schema 违约丢弃:", errorMessage(err));
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

  private observe(remote: HlcTimestamp): void {
    if (!this.clock) return;
    this.clock.observe(remote);
    const [physical, logical] = this.clock.state;
    this.store.saveHlcState(physical, logical);
  }

  private async merge(event: SyncEvent): Promise<MergeOutcome> {
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
      contentHash: await sha256Hex(content),
    });
    return "applied";
  }

  private async heartbeatOnce(): Promise<void> {
    const active = this.active;
    if (!active) return;
    try {
      const at = await this.client.heartbeat(active.relayUrl, active.deviceToken, {
        deviceId: active.deviceId,
        tier: active.tier,
        appVersion: active.appVersion,
      });
      this.store.saveLastHeartbeat(at || this.nowIso());
      this.heartbeatsTotal += 1;
    } catch (err) {
      // 心跳失败只告警（下周期重试）；成功不清他步 lastError（拉取失败的归因不被覆盖）。
      this.lastError = `heartbeat: ${errorMessage(err)}`;
    }
  }
}

function defaultSchedule(tick: () => void, intervalMs: number): () => void {
  const handle = setInterval(tick, intervalMs);
  return () => clearInterval(handle);
}

function readCursor(wire: unknown): number {
  const cursor = (wire as { cursor?: unknown } | null)?.cursor;
  return typeof cursor === "number" && Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : 0;
}

function shellAppVersion(): string {
  const config = (globalThis as { LINGXI_SHELL_CONFIG?: { versionName?: unknown } })
    .LINGXI_SHELL_CONFIG;
  return typeof config?.versionName === "string" ? config.versionName : "unknown";
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
