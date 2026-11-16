/**
 * 渲染进程健康观测（挂死/崩溃证据面，修 G4 观测缺口）。
 *
 * 覆盖三类事件：
 *  - app 'render-process-gone'：渲染进程消失（崩溃/OOM/被杀等，reason+
 *    exitCode，Electron 39 的 RenderProcessGoneDetails 无 processId 字段，
 *    进程号从事件第二参 webContents.getProcessId() 取）；
 *  - 主窗口 webContents 'unresponsive'：渲染主线程挂死（无输入事件响应）；
 *  - 主窗口 webContents 'responsive'：挂死后恢复响应。
 *
 * 证据面先行：详情落 diag-log + console.warn，不自动重启、不弹窗（处置
 * 面留待证据积累后另议）。处理函数不 import electron、日志经注入——可
 * 在 node 环境直接单测；接线（app/webContents 监听注册）在 main.ts。
 */

/** 渲染进程消失详情（Electron RenderProcessGoneDetails 的结构化子集） */
export interface RenderProcessGoneDetails {
  /** 消失原因（clean-exit/abnormal-exit/killed/crashed/oom/launch-failed/…） */
  reason: string;
  /** 退出码（launch-failed 时为平台启动失败错误码） */
  exitCode: number;
}

/** diag-log 写面签名（生产 = electron/diag-log.ts 的 logDiag） */
export type DiagLineSink = (line: string) => void;

/** 渲染进程消失：详情落盘（reason/exitCode/processId 全量在案） */
export function reportRenderProcessGone(
  details: RenderProcessGoneDetails,
  processId: number | null,
  logDiag: DiagLineSink,
): void {
  const line =
    `[renderer] render-process-gone reason=${details?.reason} ` +
    `exitCode=${details?.exitCode} processId=${processId ?? "?"}`;
  console.warn("[Electron] 渲染进程消失:", line);
  logDiag(line);
}

/** 渲染主线程挂死（unresponsive） */
export function reportWebContentsUnresponsive(
  processId: number | null,
  logDiag: DiagLineSink,
): void {
  const line = `[renderer] webContents unresponsive processId=${processId ?? "?"}`;
  console.warn("[Electron] 渲染进程无响应:", line);
  logDiag(line);
}

/** 挂死恢复响应（responsive；恢复留痕使下次挂死的告警可对账） */
export function reportWebContentsResponsive(
  processId: number | null,
  logDiag: DiagLineSink,
): void {
  const line = `[renderer] webContents responsive processId=${processId ?? "?"}`;
  console.warn("[Electron] 渲染进程恢复响应:", line);
  logDiag(line);
}
