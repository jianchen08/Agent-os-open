/**
 * 插件配置 API 服务测试
 *
 * 覆盖 task_11 P1-6 数据层：
 * - getPluginConfigs：从 schema 聚合响应提取插件配置树
 * - getPluginConfigFile：GET 配置文件（ETag 处理）
 * - savePluginConfigFile：PUT 配置文件（409 ETag 冲突处理）
 *
 * 测试策略：Mock 仅传输层（apiClient / axios），被测服务本身及其解析逻辑真实运行。
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'

// Mock 传输层（axios 实例），仅拦截 HTTP，不替代被测服务的解析逻辑。
vi.mock('@/services/api/client', () => ({
  default: {
    get: vi.fn(),
    put: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'
import {
  getPluginConfigs,
  getPluginConfigFile,
  savePluginConfigFile,
  PluginConfigConflictError,
} from '@/services/api/pluginConfig'
import type { SchemaResponse } from '@/services/api/schema'

const mockGet = vi.mocked(apiClient.get)
const mockPut = vi.mocked(apiClient.put)

/** 构造一个 axios 风格的响应对象。 */
function axResponse<T>(data: T, headers: Record<string, string> = {}): { data: T; headers: Record<string, string> } {
  return { data, headers }
}

/** 构造一个 axios 风格的错误对象（带 response）。 */
function axiosError(status: number, data: unknown, headers: Record<string, string> = {}): unknown {
  const err = Object.assign(new Error(`Request failed with status code ${status}`), {
    response: { status, data, headers },
    isAxiosError: true,
  })
  return err
}

describe('getPluginConfigs — 从 schema 提取插件配置树', () => {
  it('从 SchemaResponse.plugin_configs 返回插件配置列表', () => {
    const schema: SchemaResponse = {
      agents: [],
      pipelines: [],
      tools: [],
      routes: {},
      plugin_configs: [
        {
          plugin_id: 'connectors',
          plugin_name: '连接器',
          config_files: [
            { id: 'godot', path: 'config/external_tools/godot.yaml', label: 'Godot' },
          ],
        },
      ],
    } as unknown as SchemaResponse

    const result = getPluginConfigs(schema)

    expect(result).toHaveLength(1)
    expect(result[0].plugin_id).toBe('connectors')
    expect(result[0].config_files[0].id).toBe('godot')
  })

  it('schema 无 plugin_configs 字段时返回空数组', () => {
    const schema = {
      agents: [],
      pipelines: [],
      tools: [],
      routes: {},
    } as unknown as SchemaResponse

    expect(getPluginConfigs(schema)).toEqual([])
  })

  it('plugin_configs 为空数组时返回空数组', () => {
    const schema = {
      agents: [],
      pipelines: [],
      tools: [],
      routes: {},
      plugin_configs: [],
    } as unknown as SchemaResponse

    expect(getPluginConfigs(schema)).toEqual([])
  })
})

describe('getPluginConfigFile — GET 配置文件 + ETag', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('调用 /api/v1/plugins/{id}/config/{file_id} 并返回 data + etag（响应头）', async () => {
    mockGet.mockResolvedValue(
      axResponse(
        { plugin_id: 'connectors', file_id: 'godot', label: 'Godot', path: 'config/external_tools/godot.yaml', data: { host: 'localhost' }, etag: 'etag-from-header' },
        { etag: 'etag-from-header' },
      ),
    )

    const result = await getPluginConfigFile('connectors', 'godot')

    expect(mockGet).toHaveBeenCalledWith('/api/v1/plugins/connectors/config/godot')
    expect(result.etag).toBe('etag-from-header')
    expect(result.data.data).toEqual({ host: 'localhost' })
    expect(result.data.label).toBe('Godot')
  })

  it('响应头缺失 ETag 时回退到响应体 body.etag', async () => {
    mockGet.mockResolvedValue(
      axResponse(
        { plugin_id: 'p', file_id: 'f', data: {}, etag: 'fallback-etag' },
        {},
      ),
    )

    const result = await getPluginConfigFile('p', 'f')
    expect(result.etag).toBe('fallback-etag')
  })

  it('响应头与响应体都缺失 ETag 时返回空字符串', async () => {
    mockGet.mockResolvedValue(
      axResponse(
        { plugin_id: 'p', file_id: 'f', data: {} },
        {},
      ),
    )

    const result = await getPluginConfigFile('p', 'f')
    expect(result.etag).toBe('')
  })
})

describe('savePluginConfigFile — PUT 配置文件 + 409 冲突处理', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it('成功保存返回新 ETag，请求体携带 data + if_match', async () => {
    mockPut.mockResolvedValue(
      axResponse(
        { plugin_id: 'connectors', file_id: 'godot', etag: 'new-etag' },
        { etag: 'new-etag' },
      ),
    )

    const result = await savePluginConfigFile('connectors', 'godot', { host: 'newhost' }, 'old-etag')

    expect(mockPut).toHaveBeenCalledWith('/api/v1/plugins/connectors/config/godot', {
      data: { host: 'newhost' },
      if_match: 'old-etag',
    })
    expect(result.etag).toBe('new-etag')
  })

  it('新 ETag 优先取响应头，回退到响应体', async () => {
    mockPut.mockResolvedValue(
      axResponse(
        { plugin_id: 'p', file_id: 'f', etag: 'body-etag' },
        {},
      ),
    )

    const result = await savePluginConfigFile('p', 'f', { x: 1 }, 'prev')
    expect(result.etag).toBe('body-etag')
  })

  it('409 ETag 冲突抛出 PluginConfigConflictError（携带当前 etag），而非透传 axios 错误', async () => {
    // 后端 Conflict 响应体含 ETag mismatch 信息，响应头附当前 etag
    mockPut.mockRejectedValue(
      axiosError(409, { message: 'ETag mismatch: current=cur-etag, given="old-etag"' }, { etag: 'cur-etag' }),
    )

    await expect(savePluginConfigFile('p', 'f', { x: 1 }, 'old-etag')).rejects.toMatchObject({
      name: 'PluginConfigConflictError',
      currentEtag: 'cur-etag',
    })
  })

  it('409 冲突但响应头无 etag 时，currentEtag 为 undefined', async () => {
    mockPut.mockRejectedValue(
      axiosError(409, { message: 'ETag mismatch' }, {}),
    )

    await expect(savePluginConfigFile('p', 'f', { x: 1 }, 'old')).rejects.toMatchObject({
      name: 'PluginConfigConflictError',
    })
    try {
      await savePluginConfigFile('p', 'f', { x: 1 }, 'old')
    } catch (e) {
      expect((e as PluginConfigConflictError).currentEtag).toBeUndefined()
    }
  })

  it('非 409 错误（如 500）原样透传，不包装为冲突错误', async () => {
    const serverErr = axiosError(500, { message: 'internal' })
    mockPut.mockRejectedValue(serverErr)

    await expect(savePluginConfigFile('p', 'f', { x: 1 }, 'old')).rejects.toBe(serverErr)
  })

  it('isPluginConfigConflict 谓词正确判定冲突错误', async () => {
    mockPut.mockRejectedValue(
      axiosError(409, { message: 'mismatch' }, { etag: 'cur' }),
    )

    try {
      await savePluginConfigFile('p', 'f', { x: 1 }, 'old')
      throw new Error('should have thrown')
    } catch (e) {
      expect(PluginConfigConflictError.is(e)).toBe(true)
    }
  })

  it('无 ifMatch（首次保存）时 if_match 仍显式传递（后端要求匹配当前值）', async () => {
    mockPut.mockResolvedValue(axResponse({ etag: 'e1' }, { etag: 'e1' }))

    await savePluginConfigFile('p', 'f', { x: 1 })

    expect(mockPut).toHaveBeenCalledWith('/api/v1/plugins/p/config/f', {
      data: { x: 1 },
      if_match: undefined,
    })
  })
})
