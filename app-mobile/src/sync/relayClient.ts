/**
 * relay HTTP 客户端 —— /ext/sync_relay/** 端点面的壳侧消费（fetch 可注入）。
 *
 * 端点契约（sync_relay server.py）：enroll 兑换凭 admin JWT（Bearer，登录即
 * 入网 ADR 2026-11-02-login-as-enrollment），其余设备面端点 Bearer per-device
 * token；错误体 = {"error": {"code", "message"}}（http_json.protocol_error），
 * message 原样上抛供面板呈现。超时 10s 统一 AbortController。
 */

export class RelayError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "RelayError";
    this.status = status;
  }
}

export interface EnrollRequest {
  readonly deviceId: string
  readonly tier: string
  readonly appVersion: string
  readonly capabilities: Record<string, unknown>
}

export interface EnrollOutcome {
  readonly deviceId: string
  readonly deviceToken: string
  readonly tenantId: string
  readonly relayUrl: string
}

export interface DeviceRegistration {
  readonly deviceId: string
  readonly tier: string
  readonly appVersion: string
  readonly capabilities: Record<string, unknown>
}

export interface PullPage {
  readonly events: unknown[]
  readonly count: number
}

export interface BootstrapPage {
  readonly device: string
  readonly events: unknown[]
  readonly cursor: number
}

const REQUEST_TIMEOUT_MS = 10_000;

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export class RelayClient {
  constructor(private readonly fetchImpl: FetchLike = (...args) => fetch(...args)) {}

  /** admin JWT 直签 per-device token（POST {relay}/enroll，Bearer JWT 即凭据）。 */
  async enrollWithJwt(relayUrl: string, jwt: string, request: EnrollRequest): Promise<EnrollOutcome> {
    const body = await this.postJson(relayUrl, joinPath(relayUrl, "/enroll"), jwt, {
      device_id: request.deviceId,
      tier: request.tier,
      app_version: request.appVersion,
      capabilities: request.capabilities,
    });
    return {
      deviceId: requireString(body, "device_id", "enroll"),
      deviceToken: requireString(body, "device_token", "enroll"),
      tenantId: typeof body.tenant_id === "string" ? body.tenant_id : "",
      // 响应无 relay_url 回显：调用 URL 即事实地址。
      relayUrl,
    };
  }

  /** 设备注册（POST {relay}/devices；幂等，自举首步）。 */
  async register(relayUrl: string, token: string, registration: DeviceRegistration): Promise<void> {
    await this.postJson(relayUrl, joinPath(relayUrl, "/devices"), token, {
      device_id: registration.deviceId,
      tier: registration.tier,
      app_version: registration.appVersion,
      capabilities: registration.capabilities,
    });
  }

  /** 心跳上报（POST {relay}/devices/heartbeat）。 */
  async heartbeat(
    relayUrl: string,
    token: string,
    registration: Omit<DeviceRegistration, "capabilities">,
  ): Promise<string> {
    const body = await this.postJson(relayUrl, joinPath(relayUrl, "/devices/heartbeat"), token, {
      device_id: registration.deviceId,
      tier: registration.tier,
      app_version: registration.appVersion,
    });
    return typeof body.heartbeat_at === "string" ? body.heartbeat_at : "";
  }

  /** cursor 增量拉取（GET {relay}/events?since_cursor=，严格大于、按 cursor 升序）。 */
  async pullEvents(relayUrl: string, token: string, sinceCursor: number, limit?: number): Promise<PullPage> {
    const query = new URLSearchParams({ since_cursor: String(sinceCursor) });
    if (limit !== undefined) query.set("limit", String(limit));
    const body = await this.getJson(relayUrl, `${joinPath(relayUrl, "/events")}?${query}`, token);
    const events = Array.isArray(body.events) ? body.events : [];
    return { events, count: events.length };
  }

  /** 入网基线（GET {relay}/bootstrap，层级权威①：relay 按端位阶裁定各资产基线）。 */
  async bootstrap(relayUrl: string, token: string): Promise<BootstrapPage> {
    const body = await this.getJson(relayUrl, joinPath(relayUrl, "/bootstrap"), token);
    const events = Array.isArray(body.events) ? body.events : [];
    const cursor = body.cursor;
    return {
      device: typeof body.device === "string" ? body.device : "",
      events,
      cursor: typeof cursor === "number" && Number.isSafeInteger(cursor) && cursor >= 0 ? cursor : 0,
    };
  }

  private async getJson(relayUrl: string, url: string, token: string): Promise<Record<string, unknown>> {
    const headers: Record<string, string> = {};
    if (token) headers.Authorization = `Bearer ${token}`;
    return this.request(url, { method: "GET", headers }, relayUrl);
  }

  private async postJson(
    relayUrl: string,
    url: string,
    token: string | null,
    payload: unknown,
  ): Promise<Record<string, unknown>> {
    const headers: Record<string, string> = { "Content-Type": "application/json" };
    if (token) headers.Authorization = `Bearer ${token}`;
    return this.request(
      url,
      { method: "POST", headers, body: JSON.stringify(payload) },
      relayUrl,
    );
  }

  private async request(
    url: string,
    init: RequestInit,
    relayUrl: string,
  ): Promise<Record<string, unknown>> {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    let response: Response;
    try {
      response = await this.fetchImpl(url, { ...init, signal: controller.signal });
    } catch (err) {
      throw new RelayError(0, `relay 不可达（${describeUrl(relayUrl)}）: ${errorMessage(err)}`);
    } finally {
      clearTimeout(timer);
    }
    const text = await response.text();
    let body: unknown = null;
    try {
      body = text ? JSON.parse(text) : null;
    } catch {
      throw new RelayError(response.status, `relay 响应不是 JSON（HTTP ${response.status}）`);
    }
    if (!response.ok) {
      throw new RelayError(response.status, extractErrorMessage(body, response.status));
    }
    if (body === null || typeof body !== "object" || Array.isArray(body)) {
      throw new RelayError(response.status, "relay 响应必须是 JSON 对象");
    }
    return body as Record<string, unknown>;
  }
}

function joinPath(relayUrl: string, path: string): string {
  return relayUrl.replace(/\/+$/, "") + path;
}

function requireString(body: Record<string, unknown>, key: string, where: string): string {
  const value = body[key];
  if (typeof value !== "string" || !value) {
    throw new RelayError(502, `relay ${where} 响应缺 ${key}`);
  }
  return value;
}

function extractErrorMessage(body: unknown, status: number): string {
  const inner =
    body !== null && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>).error
      : null;
  if (inner !== null && typeof inner === "object" && !Array.isArray(inner)) {
    const message = (inner as Record<string, unknown>).message;
    if (typeof message === "string" && message) return message;
  }
  if (typeof inner === "string" && inner) return inner;
  return `relay 错误（HTTP ${status}）`;
}

function describeUrl(relayUrl: string): string {
  try {
    return new URL(relayUrl).host;
  } catch {
    return relayUrl;
  }
}

function errorMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
