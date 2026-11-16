// @ci: frontend-test
/**
 * 桌面版 WS 中继测试（2026-10-22）：渲染层连回环端点，主进程转接目标内核
 * /ws/chat 并双向转发。
 *
 * 断行为：
 *  - 转发：客户端经中继发消息 → 目标内核 WS 收到 → 回程经中继回到客户端；
 *  - 边界：目标未声明 → 不给 URL；声明 A 却请求 B → 不给 URL（非开放代理）。
 */
import { WebSocket, WebSocketServer } from "ws";
import type { AddressInfo } from "node:net";
import { afterEach, describe, expect, it } from "vitest";

import { startWsRelay } from "../ws-relay";

const cleanups: (() => void)[] = [];

afterEach(() => {
  while (cleanups.length) {
    const fn = cleanups.pop();
    try {
      fn?.();
    } catch {
      // 清理失败不阻断断言
    }
  }
});

/** 起一个回显型 WS 服务端（模拟内核 /ws/chat）；端口在 listening 后取 */
async function startUpstream(): Promise<{ target: string; close: () => void }> {
  const wss = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  wss.on("connection", (socket) => {
    socket.on("message", (data) => {
      socket.send(`echo:${data.toString()}`);
    });
  });
  cleanups.push(() => {
    for (const client of wss.clients) client.terminate();
    wss.close();
  });
  await new Promise<void>((resolve) => wss.once("listening", () => resolve()));
  const port = (wss.address() as AddressInfo).port;
  return { target: `http://127.0.0.1:${port}`, close: () => wss.close() };
}

describe("ws-relay（桌面版远端实时通道中继）", () => {
  it("回环客户端 → 中继 → 目标内核 WS：消息双向转发", async () => {
    const upstream = await startUpstream();
    const relay = startWsRelay();
    cleanups.push(() => relay.close());
    await relay.ready;
    relay.setAllowedTarget(upstream.target);

    const url = relay.buildUrl({
      target: upstream.target,
      ticket: "tk-1",
      version: "3.0.0",
    });
    expect(url).toBeTruthy();
    expect(url).toMatch(/^ws:\/\/127\.0\.0\.1:\d+\/relay\?/);

    const client = new WebSocket(url as string);
    cleanups.push(() => client.terminate());
    // 中继先发 {"type":"relay/open"} 通知帧，再转发上游消息——累计到回显为止
    const received = await new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error("中继回程超时")), 8000);
      let seen = "";
      client.on("message", (data) => {
        seen += data.toString();
        if (seen.includes("echo:hello")) {
          clearTimeout(timer);
          resolve(seen);
        }
      });
      client.on("error", (err) => {
        clearTimeout(timer);
        reject(err);
      });
      client.on("open", () => client.send("hello"));
    });
    expect(received).toContain("echo:hello");
  });

  it("目标未声明 → 不给 URL（非开放代理）", async () => {
    const upstream = await startUpstream();
    const relay = startWsRelay();
    cleanups.push(() => relay.close());
    await relay.ready;

    expect(relay.buildUrl({ target: upstream.target, ticket: "tk" })).toBeNull();
  });

  it("声明 A 却请求 B → 不给 URL（跨目标越权拒绝）", async () => {
    const upstream = await startUpstream();
    const relay = startWsRelay();
    cleanups.push(() => relay.close());
    await relay.ready;
    relay.setAllowedTarget(upstream.target);

    expect(
      relay.buildUrl({ target: "http://10.0.0.9:9100", ticket: "tk" }),
    ).toBeNull();
  });
});
