/**
 * PluginsSettingsPage 测试族共享数据与桩。
 *
 * 只承载纯数据与 vi.fn 桩实例；vi.mock 声明因提升语义必须留在各测试文件
 * 原地（经 vi.mock 工厂内动态 import 引用本模块的桩）。
 */
import { vi } from 'vitest'

/** apiClient 桩（default 导出形状）：get 由各用例 mockImplementation/mockRejectedValue 驱动 */
export const apiClientStub = {
  get: vi.fn(),
  put: vi.fn(),
}

/** sonner toast 桩 */
export const toastStub = {
  error: vi.fn(),
  success: vi.fn(),
  warning: vi.fn(),
}

/** 插件注册表健康载荷中的 monitoring_service 条目（失败态与主链共用同源数据） */
export const MONITORING_PLUGIN = {
  plugin_id: 'monitoring_service',
  name: 'Monitoring Service',
  description: '系统指标采集与监控告警',
  config_type: 'system',
  host_type: 'sidecar',
  version: '1.0.0',
  enabled: true,
  activation: 'lazy',
  status: 'active',
  config_files: [],
  has_contributes: false,
  has_http_endpoints: true,
  error: null,
}
