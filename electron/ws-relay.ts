/**
 * 桌面版 WebSocket 中继（2026-10-22 用户实测「连远端内核实时通道不通」修复）。
 *
 * 根因：打包件渲染页 origin=app://（registerSchemesAsPrivileged secure=true=安全
 * 源），安全源发 ws:// 远端属**混合内容** → 渲染层 WebSocket 握手被浏览器拦
 * （fetch/XHR 已被 allow-running-insecure-content 放行，WS 不在其列——CDP 实测：
 * 到远端的 ws:// 6 秒不 open 不 error，同刻本机 ws://127.0.0.1:9101 正常 101）。
 *
 * 方案：主进程起一个**仅回环可达**的 WS 端点，渲染层连它（回环=可信源，混合内容
 * 豁免），主进程再连真正的远端内核 WS（/ws/chat?ticket=…）并双向转发。内核零改动。
 *
 * 安全边界：中继**不是开放代理**——只转发到渲染层显式声明的当前内核基址
 * （setAllowedTarget，由 ws:relay:allow 告知）；未声明/不匹配一律拒绝并关闭。
 */
import { WebSocket, WebSocketServer } from "ws";
import type { RawData } from "ws";
import type { IncomingMessage } from "node:http";

/** 内核全局 WS 端点路径（kernel/crates/api/src/server.rs: /ws/chat） */
const KERNEL_WS_PATH = "/ws/chat";

export interface WsRelayParams {
  /** 目标内核基址（http/https） */
  target?: unknown;
  /** 一次性握手票据 */
  ticket?: unknown;
  /** 协议版本 */
  version?: unknown;
  /** 断线补漏游标 */
  last_sequence?: unknown;
}

export interface WsRelayHandle {
  /** 回环监听端口（0=未就绪） */
  port: number;
  /** 监听就绪承诺（IPC 取 URL 前 await，端口必已分配） */
  ready: Promise<number>;
  /** 声明当前允许转发的目标基址（null=不放行） */
  setAllowedTarget(target: string | null): void;
  /** 构造中继 URL；目标未授权/参数非法返回 null */
  buildUrl(params: WsRelayParams): string | null;
  close(): void;
}

/** 基址归一（去尾斜杠、小写 host、保留端口与路径前缀） */
function normalizeTarget(raw: string): string | null {
  try {
    const url = new URL(raw);
    if (url.protocol !== "http:" && url.protocol !== "https:") return null;
    return `${url.protocol}//${url.host}${url.pathname.replace(/\/+$/, "")}`;
  } catch {
    return null;
  }
}

/** 目标基址 → 上游 WS URL（含内核端点路径与握手参数） */
function upstreamWsUrl(
  target: string,
  ticket: string,
  version: string,
  lastSequence?: string,
): string {
  const url = new URL(target);
  url.protocol = url.protocol === "https:" ? "wss:" : "ws:";
  url.pathname = `${url.pathname.replace(/\/+$/, "")}${KERNEL_WS_PATH}`;
  const query = new URLSearchParams({ ticket, version });
  if (lastSequence) query.set("last_sequence", lastSequence);
  url.search = query.toString();
  return url.toString();
}

/**
 * 启动回环 WS 中继。端口 0（随机）——避免与其它本机服务撞端口；端口经
 * IPC 交给渲染层。
 */
export function startWsRelay(): WsRelayHandle {
  const wss = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  let allowedTarget: string | null = null;
  const ready = new Promise<number>((resolve) => {
    wss.once("listening", () => resolve(port()));
  });

  const setAllowedTarget = (target: string | null): void => {
    allowedTarget = target ? normalizeTarget(target) : null;
  };

  const buildUrl = (params: WsRelayParams): string | null => {
    if (!allowedTarget) return null;
    if (typeof params.target !== "string") return null;
    if (normalizeTarget(params.target) !== allowedTarget) return null;
    if (typeof params.ticket !== "string" || !params.ticket) return null;
    const version = typeof params.version === "string" ? params.version : "";
    const lastSequence =
      typeof params.last_sequence === "number" ? String(params.last_sequence) : undefined;
    const query = new URLSearchParams({
      target: allowedTarget,
      ticket: params.ticket,
    });
    if (version) query.set("version", version);
    if (lastSequence) query.set("last_sequence", lastSequence);
    return `ws://127.0.0.1:${port()}/relay?${query.toString()}`;
  };

  const port = (): number => {
    const address = wss.address();
    return typeof address === "object" && address ? address.port : 0;
  };

  wss.on("connection", (client: WebSocket, req: IncomingMessage) => {
    const requestUrl = new URL(req.url ?? "/", "ws://127.0.0.1");
    const query = requestUrl.searchParams;
    const target = normalizeTarget(query.get("target") ?? "");
    const ticket = query.get("ticket") ?? "";
    if (!target || !allowedTarget || target !== allowedTarget || !ticket) {
      // 未授权目标/缺票据：拒绝（不转发任何字节）
      client.close(1008, "relay target not allowed");
      return;
    }
    const version = query.get("version") ?? "";
    const lastSequence = query.get("last_sequence") ?? undefined;
    const upstream = new WebSocket(upstreamWsUrl(target, ticket, version, lastSequence));
    // 上游未就绪时客户端可能已发首帧（客户端 open 即发）——缓冲到上游 open 后补发，
    // 否则首帧被丢（表现为「连上了但收不到任何推送」）。
    const pending: RawData[] = [];
    upstream.on("open", () => {
      client.send(JSON.stringify({ type: "relay/open" }));
      for (const queued of pending.splice(0)) upstream.send(queued as Buffer);
    });
    upstream.on("message", (data: RawData) => {
      if (client.readyState === 1) client.send(data as Buffer);
    });
    upstream.on("error", () => {
      if (client.readyState === 1) client.close(1011, "upstream error");
    });
    upstream.on("close", (code, reason) => {
      if (client.readyState === 1) {
        client.close(code >= 1000 ? code : 1011, reason?.toString?.() ?? "upstream closed");
      }
    });
    client.on("message", (data: RawData) => {
      if (upstream.readyState === 1) upstream.send(data as Buffer);
      else pending.push(data);
    });
    client.on("close", () => {
      if (upstream.readyState === 1) upstream.close(1000, "client closed");
    });
  });

  return {
    get port() {
      return port();
    },
    ready,
    setAllowedTarget,
    buildUrl,
    close: () => {
      for (const client of wss.clients) client.terminate();
      wss.close();
    },
  } as WsRelayHandle;
}
