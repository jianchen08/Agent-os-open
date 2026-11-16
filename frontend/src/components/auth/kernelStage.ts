/**
 * 壳桥 KernelHost.status() 的前端消费面（连接前内核真实状态，用户令 2026-10-26）。
 * stage/extract_entries/venv_done 为 BootProgress 进度面（38+ 包才有，旧包退化
 * 按 seeded/initialized 布尔推断）。
 */
export interface ShellKernelStatus {
  running?: boolean
  seeded?: boolean
  initialized?: boolean
  stage?: string
  extract_entries?: number
  venv_done?: number
  /** 启动失败面（BootProgress 透出，非空=失败）：登录页即时呈现不再傻等 */
  boot_error?: string
  boot_error_stage?: string
}

/** 失败阶段 → 中文（错误文案用） */
export function bootStageLabel(stage?: string): string {
  switch (stage) {
    case 'extract':
      return '解压'
    case 'assemble':
      return '环境装配'
    case 'starting':
      return '内核拉起'
    default:
      return '启动'
  }
}

/** status → 启动阶段文案（解压 → 装配 → 拉起，与 bootChain 实序一致；解压/装配
 *  带真实计数进度，无先验总数不伪装百分比） */
export function kernelStageText(status: ShellKernelStatus | null): string {
  if (!status) return '本机内核启动中'
  if (status.running) return '本机内核已就绪，登录中'
  if (status.stage === 'extract' || (!status.seeded && status.stage === undefined)) {
    return typeof status.extract_entries === 'number' && status.extract_entries > 0
      ? `本机内核资源解压中（已解压 ${status.extract_entries} 个文件）`
      : '本机内核资源解压中'
  }
  if (status.stage === 'assemble') {
    return typeof status.venv_done === 'number' && status.venv_done > 0
      ? `本机环境装配中（已完成 ${status.venv_done} 个模块）`
      : '本机环境装配中'
  }
  if (!status.seeded) return '本机内核资源解压中'
  if (!status.initialized) return '本机环境装配中'
  return '本机内核启动中'
}
