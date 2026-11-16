/**
 * 手机端同步状态面板（壳侧）—— Shadow DOM 隔离挂载 + 浮动入口小钮。
 *
 * 设置中枢唯一 UI 在共享前端（widget 承载），壳侧零前端改动约束下设此独立
 * 面板承载状态面：入网经前端登录自动完成（登录即入网，ADR
 * 2026-11-02-login-as-enrollment，壳侧无输入面）；面板呈现配对状态
 * （device_id、relay、游标、最后心跳、通道）/ 立即同步 / 解除配对。入口小钮
 * 常驻右下（safe-inset 变量避让，壳 insets 注入同源）。
 */

import type { EngineStatus, SyncEngine } from "./syncEngine";

export interface PanelDeps {
  engine: SyncEngine
  credentialBackend: () => string
}

export class SyncPanel {
  private readonly engine: SyncEngine;
  private readonly credentialBackend: () => string;
  private host: HTMLDivElement | null = null;
  private panel: HTMLDivElement | null = null;
  private chip: HTMLButtonElement | null = null;

  constructor(deps: PanelDeps) {
    this.engine = deps.engine;
    this.credentialBackend = deps.credentialBackend;
  }

  /** 挂载浮动入口（幂等：已挂载先卸）。 */
  mount(root: ParentNode = document.body): void {
    this.unmount();
    const host = document.createElement("div");
    host.id = "lingxi-sync-panel-host";
    const chip = document.createElement("button");
    chip.type = "button";
    chip.id = "lingxi-sync-chip";
    chip.setAttribute("data-testid", "sync-shell-chip");
    chip.setAttribute("aria-label", "同步设置");
    chip.textContent = "⟳";
    chip.addEventListener("click", () => this.toggle());
    host.appendChild(chip);
    root.appendChild(host);
    this.host = host;
    this.chip = chip;
  }

  unmount(): void {
    this.close();
    this.host?.remove();
    this.host = null;
    this.chip = null;
  }

  toggle(): void {
    if (this.panel) this.close();
    else void this.open();
  }

  async open(): Promise<void> {
    this.close();
    const host = this.host ?? document.body;
    const panel = document.createElement("div");
    panel.id = "lingxi-sync-panel";
    panel.setAttribute("data-testid", "sync-shell-panel");
    const shadow = panel.attachShadow({ mode: "open" });
    shadow.innerHTML = TEMPLATE;
    host.appendChild(panel);
    this.panel = panel;

    this.requireEl<HTMLButtonElement>(shadow, "sync-now-button").addEventListener("click", () =>
      void this.handleSyncNow(),
    );
    this.requireEl<HTMLButtonElement>(shadow, "unpair-button").addEventListener("click", () =>
      void this.handleUnpair(),
    );
    this.requireEl<HTMLButtonElement>(shadow, "panel-close-button").addEventListener("click", () =>
      this.close(),
    );
    this.refresh();
  }

  close(): void {
    this.panel?.remove();
    this.panel = null;
  }

  /** 现读引擎状态重绘（打开时与每个动作完成后调用）。 */
  refresh(): void {
    const shadow = this.panel?.shadowRoot;
    if (!shadow) return;
    const status = this.engine.status();
    this.requireEl<HTMLParagraphElement>(shadow, "sync-shell-status").textContent =
      describeStatus(status);
    this.requireEl<HTMLParagraphElement>(shadow, "sync-shell-detail").textContent =
      describeDetail(status, this.credentialBackend());
    this.requireEl<HTMLDivElement>(shadow, "paired-actions").style.display = status.paired
      ? ""
      : "none";
  }

  private async handleSyncNow(): Promise<void> {
    const shadow = this.requireShadow();
    const result = this.requireEl<HTMLParagraphElement>(shadow, "sync-now-result");
    result.textContent = "同步中…";
    try {
      await this.engine.syncNow();
      result.textContent = "已完成本轮同步";
    } catch (err) {
      result.textContent = err instanceof Error ? err.message : String(err);
    }
    this.refresh();
  }

  private async handleUnpair(): Promise<void> {
    const shadow = this.requireShadow();
    if (!window.confirm("解除配对将清除本机凭据与同步进度，确认？")) return;
    await this.engine.unpair();
    this.refresh();
  }

  private requireShadow(): ShadowRoot {
    if (!this.panel?.shadowRoot) throw new Error("sync panel not open");
    return this.panel.shadowRoot;
  }

  private requireEl<T extends Element>(root: ShadowRoot | Document, testId: string): T {
    const el = root.querySelector(`[data-testid="${testId}"]`);
    if (!el) throw new Error(`sync panel element missing: ${testId}`);
    return el as T;
  }
}

function describeStatus(status: EngineStatus): string {
  if (!status.paired) {
    return "未配对：登录后自动入网（admin 登录即完成设备入网）";
  }
  return (
    "已配对" +
    (status.bootstrapDone ? "（已自举）" : "（自举待完成：relay 不可达时自动重试）")
  );
}

function describeDetail(status: EngineStatus, credentialBackend: string): string {
  const parts: string[] = [];
  if (status.paired) {
    parts.push(`设备 ${status.deviceId ?? "?"}`, `档位 ${status.tier}`);
    if (status.relayUrl) parts.push(`relay ${status.relayUrl}`);
    parts.push(`游标 ${status.cursor ?? "-"}`);
    parts.push(`最后心跳 ${status.lastHeartbeatAt ?? "无"}`);
  }
  if (status.lastError) parts.push(`最近错误 ${status.lastError}`);
  parts.push(`凭据通道 ${credentialBackend}`);
  return parts.join(" · ");
}

const TEMPLATE = `
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
  <button type="button" class="close" data-testid="panel-close-button" aria-label="关闭同步设置">×</button>
  <h2>多端同步（手机端）</h2>
  <p data-testid="sync-shell-status"></p>
  <p class="hint" data-testid="sync-shell-detail"></p>
  <div data-testid="paired-actions">
    <div class="row">
      <button type="button" class="act primary" data-testid="sync-now-button">立即同步</button>
      <button type="button" class="act" data-testid="unpair-button">解除配对</button>
    </div>
    <p data-testid="sync-now-result"></p>
  </div>
  <p class="hint">同步范围：config 配置面下行应用（HLC-LWW 后改赢）+ 30s 心跳；本机变更上行捕获在后续版本提供。入网经登录自动完成，无需输码。</p>
</div>
`;

/** 浮动小钮样式（chip 挂在 light DOM，样式内联注入一次）。 */
export function injectChipStyle(root: ParentNode): void {
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
