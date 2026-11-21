/**
 * 真回环内核桩（electron/__tests__ 共享夹具）：app:// 回源代理链路的测试
 * 用真 http 服务器收请求（关键路径真实依赖，防拟合）——断「请求到达哪个
 * 端口/路径/头/体」，不断「哪个客户端函数被调用」。
 */

import * as http from "http";
import type { AddressInfo } from "net";

/** 测试服务端收到的请求记录（method/url/headers/body 全量） */
export interface RecordedRequest {
  method: string;
  url: string;
  headers: http.IncomingHttpHeaders;
  body: string;
}

export interface KernelStubResponse {
  status: number;
  body: string;
  headers?: Record<string, string>;
}

export interface KernelStub {
  port: number;
  requests: RecordedRequest[];
  close: () => Promise<void>;
}

/**
 * 起真回环 http 服务器充当内核桩（127.0.0.1 随机端口）。
 * 默认响应 200 application/json {"ok":true}；respond 可定制状态/头/体。
 */
export async function startKernelStub(
  respond: (req: http.IncomingMessage, body: string) => KernelStubResponse = () => ({
    status: 200,
    body: JSON.stringify({ ok: true }),
  }),
): Promise<KernelStub> {
  const requests: RecordedRequest[] = [];
  const server = http.createServer((req, res) => {
    const chunks: Buffer[] = [];
    req.on("data", (c: Buffer) => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf-8");
      requests.push({
        method: req.method ?? "",
        url: req.url ?? "",
        headers: req.headers,
        body,
      });
      const out = respond(req, body);
      res.writeHead(out.status, out.headers ?? { "Content-Type": "application/json" });
      res.end(out.body);
    });
  });
  await new Promise<void>((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve());
  });
  const port = (server.address() as AddressInfo).port;
  return {
    port,
    requests,
    close: () =>
      new Promise<void>((resolve) => {
        server.close(() => resolve());
      }),
  };
}
