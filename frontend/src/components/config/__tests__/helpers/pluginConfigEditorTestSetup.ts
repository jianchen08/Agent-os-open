/**
 * PluginConfigEditor 测试家族共享桩：API/注册表/Toast 单例句柄 + vi.mock 工厂。
 * 曾在 branches/residual 两个测试文件逐字复制 31 行测试头（jscpd 克隆门禁）。
 * vitest 按测试文件隔离环境，单例句柄跨文件不串扰。
 */
import { vi } from 'vitest'

export const mockGet = vi.fn()
export const mockSave = vi.fn()
export const mockIsConflict = vi.fn()
export const toastError = vi.fn()
export const toastSuccess = vi.fn()

export type MappingFile = { id: string; path: string; label: string; target?: string; fields?: unknown[] }
export const mockGetMappings = vi.fn<(pluginId: string) => MappingFile[]>(() => [])

export function pluginConfigApiMock() {
  return {
    getPluginConfigFile: (...args: unknown[]) => mockGet(...args),
    savePluginConfigFile: (...args: unknown[]) => mockSave(...args),
    isPluginConfigConflict: (e: unknown) => mockIsConflict(e),
  }
}

export function contributionRegistryMock() {
  return {
    contributionRegistry: { getPluginConfigFiles: (pluginId: string) => mockGetMappings(pluginId) },
  }
}

export function sonnerMock() {
  return {
    toast: {
      error: (...a: unknown[]) => toastError(...a),
      success: (...a: unknown[]) => toastSuccess(...a),
    },
  }
}
