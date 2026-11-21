#!/usr/bin/env node
"use strict";

/**
 * 前端 L2 浏览器探针（verify_host.py 调用，不单独对用户暴露）。
 *
 * ADR 2026-11-03 §D4 无壳车道：装机前端本体不开第二实例——静态伺服前端 dist
 * （dev=<repo>/frontend/dist，装机=<安装位>/resources/frontend/dist），
 * 测试侧反代 /api、/ext、/ws 到 scratch 内核端口，Playwright 无头浏览器
 * 登录（注入 refresh_token，前端 access 仅内存持有）→ 截图 → 断言。
 *
 * 输入：argv[2] = JSON 配置路径
 *   {repo_root, dist_dir, out_dir, plugin_dir, kernel_base, kernel_host,
 *    kernel_port, admin_password}
 * 输出：<out_dir>/result.json + <out_dir>/shots/*.png
 *
 * 退出码：0=全部通过；2=断言红；1=探针自身失败（错误写 result.json）。
 */

const fs = require("node:fs");
const http = require("node:http");
const net = require("node:net");
const path = require("node:path");
const { createRequire } = require("node:module");

const MIME = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".gif": "image/gif",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".map": "application/json; charset=utf-8",
};

function loadPlaywright(repoRoot) {
  const frontendPkg = path.join(repoRoot, "frontend", "package.json");
  if (!fs.existsSync(frontendPkg)) {
    throw new Error(`找不到 ${frontendPkg}（浏览器车道需要仓内 frontend/node_modules/playwright）`);
  }
  return createRequire(frontendPkg)("playwright");
}

function pathnameOf(url) {
  try {
    return new URL(url, "http://placeholder").pathname;
  } catch {
    return "/";
  }
}

function serveStatic(req, res, distDir) {
  const pathname = decodeURIComponent(pathnameOf(req.url));
  const root = path.resolve(distDir);
  let target = path.resolve(root, "." + pathname);
  if (!target.startsWith(root)) {
    res.writeHead(403);
    res.end("forbidden");
    return;
  }
  if (fs.existsSync(target) && fs.statSync(target).isDirectory()) {
    target = path.join(target, "index.html");
  }
  if (!fs.existsSync(target)) {
    if (path.extname(pathname)) {
      res.writeHead(404);
      res.end("not found");
      return;
    }
    target = path.join(root, "index.html"); // SPA 路由回退
  }
  const ext = path.extname(target).toLowerCase();
  res.writeHead(200, {
    "content-type": MIME[ext] || "application/octet-stream",
    "cache-control": "no-store",
  });
  fs.createReadStream(target).pipe(res);
}

function proxyHttp(req, res, kernel) {
  const headers = { ...req.headers, host: `${kernel.host}:${kernel.port}` };
  const upstream = http.request(
    { host: kernel.host, port: kernel.port, path: req.url, method: req.method, headers },
    (upstreamRes) => {
      res.writeHead(upstreamRes.statusCode || 502, upstreamRes.headers);
      upstreamRes.pipe(res);
    },
  );
  upstream.on("error", (err) => {
    res.writeHead(502, { "content-type": "text/plain; charset=utf-8" });
    res.end(`proxy error: ${err.message}`);
  });
  req.pipe(upstream);
}

function tunnelWs(req, socket, head, kernel) {
  const upstream = net.connect(kernel.port, kernel.host, () => {
    const headers = [];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      const key = req.rawHeaders[i];
      const value = req.rawHeaders[i + 1];
      headers.push(`${key}: ${key.toLowerCase() === "host" ? `${kernel.host}:${kernel.port}` : value}`);
    }
    upstream.write(`${req.method} ${req.url} HTTP/1.1\r\n${headers.join("\r\n")}\r\n\r\n`);
    if (head && head.length) upstream.write(head);
    upstream.pipe(socket);
    socket.pipe(upstream);
  });
  upstream.on("error", () => socket.destroy());
  socket.on("error", () => upstream.destroy());
}

function createProxyServer(distDir, kernel) {
  const server = http.createServer((req, res) => {
    const pathname = pathnameOf(req.url);
    if (pathname.startsWith("/api/") || pathname.startsWith("/ext/")) {
      proxyHttp(req, res, kernel);
      return;
    }
    serveStatic(req, res, distDir);
  });
  server.on("upgrade", (req, socket, head) => {
    if (!pathnameOf(req.url).startsWith("/ws")) {
      socket.destroy();
      return;
    }
    tunnelWs(req, socket, head, kernel);
  });
  return server;
}

function readPluginManifest(pluginDir) {
  const file = path.join(pluginDir, "plugin.json");
  if (!fs.existsSync(file)) return {};
  try {
    return JSON.parse(fs.readFileSync(file, "utf-8"));
  } catch {
    return {};
  }
}

function sanitize(value) {
  return String(value || "page").replace(/[^A-Za-z0-9_.-]+/g, "_").slice(0, 60);
}

async function screenshot(page, shotsDir, name) {
  const file = path.join(shotsDir, name);
  await page.screenshot({ path: file, fullPage: true });
  return file;
}

async function main() {
  const configPath = process.argv[2];
  if (!configPath) throw new Error("用法: node frontend_probe.cjs <config.json>");
  const cfg = JSON.parse(fs.readFileSync(configPath, "utf-8"));
  const outDir = cfg.out_dir;
  const shotsDir = path.join(outDir, "shots");
  fs.mkdirSync(shotsDir, { recursive: true });
  const result = { checks: [], screenshots: [], console_errors: [], url: "", error: null };
  const finish = (code) => {
    fs.writeFileSync(path.join(outDir, "result.json"), JSON.stringify(result, null, 1), "utf-8");
    process.exitCode = code;
  };

  const { chromium } = loadPlaywright(cfg.repo_root);
  const kernel = { host: cfg.kernel_host, port: cfg.kernel_port };
  const server = createProxyServer(cfg.dist_dir, kernel);
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(0, "127.0.0.1", resolve);
  });
  const origin = `http://127.0.0.1:${server.address().port}`;
  let browser = null;
  try {
    browser = await chromium.launch({ headless: true });
    const context = await browser.newContext({
      viewport: { width: 1440, height: 900 },
      locale: "zh-CN",
    });
    const loginResp = await context.request.post(
      `${cfg.kernel_base}/api/v1/auth/login`,
      { data: { username: "admin", password: cfg.admin_password } },
    );
    if (!loginResp.ok()) throw new Error(`登录失败 HTTP ${loginResp.status()}`);
    const tokens = await loginResp.json();
    if (!tokens.refresh_token) {
      throw new Error("登录响应缺 refresh_token（浏览器车道需要刷新令牌落盘键）");
    }
    // 刷新令牌轮换：app 首次加载会把服务端轮换后的新令牌写回 localStorage，
    // init script 在每次整页导航都会重跑——只在缺失时注入，避免用已作废的
    // 旧令牌覆盖新令牌（否则深链导航后 refresh 连环 401、app 停在加载态）。
    await context.addInitScript((token) => {
      try {
        if (!localStorage.getItem("refresh_token")) {
          localStorage.setItem("refresh_token", token);
        }
      } catch {
        /* 存储不可用时由 app shell 断言暴露 */
      }
    }, tokens.refresh_token);

    const page = await context.newPage();
    const pageErrors = [];
    page.on("pageerror", (err) => pageErrors.push(String(err)));
    page.on("console", (msg) => {
      if (msg.type() === "error" && result.console_errors.length < 50) {
        result.console_errors.push(msg.text());
      }
    });

    const homeResp = await page.goto(`${origin}/`, {
      waitUntil: "domcontentloaded",
      timeout: 30000,
    });
    await page.waitForTimeout(3500);
    result.url = page.url();
    const authGate = await page.locator('[data-testid="auth-gate"]').count();
    let shellReady = false;
    try {
      await page.waitForSelector('[data-testid="sidebar-panel"]', { timeout: 15000 });
      shellReady = true;
    } catch {
      shellReady = false;
    }
    result.screenshots.push(
      await screenshot(page, shotsDir, "home.png"),
    );
    result.checks.push({
      name: "frontend_app_shell",
      pass: authGate === 0 && shellReady && !!homeResp && homeResp.status() < 400,
      detail: `url=${result.url} status=${homeResp ? homeResp.status() : "?"} ` +
        `auth_gate=${authGate} shell=${shellReady}`,
    });

    const pages = (readPluginManifest(cfg.plugin_dir).contributes || {}).pages || [];
    for (const spec of pages.slice(0, 2)) {
      if (!spec || typeof spec.path !== "string") continue;
      const before = pageErrors.length;
      const resp = await page.goto(`${origin}${spec.path}`, {
        waitUntil: "domcontentloaded",
        timeout: 30000,
      });
      // 插件页 = lazy route + schema 异步：等 PluginPageRenderer 落定
      // （plugin-page-root 渲染完成 / not-found / 超时）+ 页面内部 webview 渲染帧
      try {
        await page.waitForSelector(
          '[data-testid="plugin-page-root"], [data-testid="plugin-page-not-found"]',
          { timeout: 20000 },
        );
      } catch {
        // 超时由下方计数如实记红
      }
      // 深链路由的 plugin-page-root 只有 min-h-screen，webview 的 h-full 拿不到
      // 确定高度（真实入口在 workspace 面板里高度由布局给出）——测试侧补 100vh
      // 等价高度，让 webview 按真实形态铺满，截图/尺寸断言才有意义。
      await page.addStyleTag({
        content: '[data-testid="plugin-page-root"]{height:100vh;}',
      }).catch(() => {});
      // webview 内容 = 前端带 Bearer fetch /ext HTML → srcDoc iframe（首次要等
      // sidecar 拉起 + venv 解析），等 iframe 落定或加载占位消失，最多 45s。
      try {
        await page.waitForFunction(() => {
          const root = document.querySelector('[data-testid="plugin-page-root"]');
          if (!root) return false;
          return root.querySelector("iframe") !== null;
        }, { timeout: 45000 });
      } catch {
        // 超时由下方计数如实记红
      }
      await page.waitForTimeout(1000);
      result.url = page.url();
      const rootCount = await page.locator('[data-testid="plugin-page-root"]').count();
      const notFound = await page.locator('[data-testid="plugin-page-not-found"]').count();
      const stillLoading = await page.locator('[data-testid="plugin-page-loading"]').count();
      const iframe = page.locator('[data-testid="plugin-page-root"] iframe').first();
      const iframeCount = await iframe.count();
      const webviewLoading = await page.getByText("加载 Webview...", { exact: true }).count();
      let iframeBox = null;
      let frameNodes = 0;
      if (iframeCount > 0) {
        iframeBox = await iframe.boundingBox();
        const child = page.frames().find((f) => f !== page.mainFrame() && f.parentFrame() === page.mainFrame());
        if (child) {
          try {
            frameNodes = await child.evaluate(() => {
              const body = document.body;
              return body ? body.querySelectorAll("*").length : 0;
            });
          } catch {
            frameNodes = 0;
          }
        }
        try {
          const iframeShot = path.join(shotsDir, `iframe_${sanitize(spec.id || spec.path)}.png`);
          await iframe.screenshot({ path: iframeShot, timeout: 10000 });
          result.screenshots.push(iframeShot);
        } catch {
          // iframe 内部有动画/布局抖动时元素截图可能超时；页面级截图仍是主证据
        }
      }
      const shotPath = await screenshot(
        page, shotsDir, `page_${sanitize(spec.id || spec.path)}.png`,
      );
      result.screenshots.push(shotPath);
      const boxOk = !!iframeBox && iframeBox.width > 0 && iframeBox.height > 0;
      result.checks.push({
        name: `frontend_plugin_page:${spec.id || spec.path}`,
        pass: !!resp && resp.status() < 400 &&
          rootCount > 0 && notFound === 0 && stillLoading === 0 &&
          iframeCount > 0 && webviewLoading === 0 && boxOk && frameNodes > 0 &&
          pageErrors.length === before,
        detail: `path=${spec.path} status=${resp ? resp.status() : "?"} ` +
          `root=${rootCount} not_found=${notFound} loading=${stillLoading} ` +
          `iframe=${iframeCount} webview_loading=${webviewLoading} ` +
          `box=${iframeBox ? `${Math.round(iframeBox.width)}x${Math.round(iframeBox.height)}` : "?"} ` +
          `frame_nodes=${frameNodes} page_errors+${pageErrors.length - before}`,
      });
    }

    const themes = (readPluginManifest(cfg.plugin_dir).contributes || {}).themes || [];
    if (themes.length) {
      const themeVar = await page.evaluate(() => {
        const probe = ["--ds-bg-canvas", "--ds-text-primary", "--ds-color-bg"]
          .map((key) => `${key}=${getComputedStyle(document.documentElement).getPropertyValue(key).trim()}`)
          .filter((item) => !item.endsWith("="));
        return probe.join(", ") || "（:root 无 --ds-* 变量）";
      });
      result.checks.push({
        name: "frontend_theme_declared",
        pass: true,
        detail: `声明主题 ${themes.length} 个（注入面见 screenshot）：${themeVar}`,
      });
    }

    result.checks.push({
      name: "frontend_no_page_errors",
      pass: pageErrors.length === 0,
      detail: pageErrors.length ? pageErrors.slice(0, 3).join(" | ") : "无未捕获异常",
    });
    finish(result.checks.every((c) => c.pass) ? 0 : 2);
  } catch (err) {
    result.error = err && err.message ? err.message : String(err);
    finish(1);
  } finally {
    if (browser) await browser.close().catch(() => {});
    await new Promise((resolve) => server.close(resolve));
  }
}

main().catch((err) => {
  try {
    const cfg = JSON.parse(fs.readFileSync(process.argv[2], "utf-8"));
    fs.writeFileSync(
      path.join(cfg.out_dir, "result.json"),
      JSON.stringify({ checks: [], error: String(err), console_errors: [], screenshots: [] }, null, 1),
      "utf-8",
    );
  } catch {
    // 配置本身不可读时只在 stderr 报
  }
  console.error("frontend_probe 失败:", err);
  process.exitCode = 1;
});
