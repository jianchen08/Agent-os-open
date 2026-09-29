// @feature: FP-T12 前端适配(LLM 配置内核单源面) | @ci: frontend-test
/**
 * config.ts 内核单一配置面服务层测试（2026-09-28 批次 A1）。
 *
 * 行为契约（断输入→输出/副作用，mock 打在 apiClient axios 层——pluginConfig
 * 走真实代码，覆盖 GET→改树→PUT 组装）：
 * - getLLMConfig：文件视图映射 + has_key/env_var 派生（${VAR} 占位按用户
 *   .env 掩码视图判定；磁盘明文 '****' 视为已配置；空未配置）
 * - saveDefaults：patch 合并进 defaults 并整文件 PUT（If-Match 乐观锁）
 * - addModel：唯一 id 直增；同 provider 同 model_name 真重复报错；跨
 *   provider 同名派生 `<id>-<provider>`（序号避让），绝不覆盖既有条目
 * - updateProviderConfig：明文 key → PUT /api/v1/config/env + yaml 占位符；
 *   掩码值剔除不污染；keys 按索引合并保留未提交字段
 * - addProvider/deleteProvider：树编辑 + 整文件 PUT
 */

import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockGet, mockPut } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockPut: vi.fn(),
}))

vi.mock('@/services/api/client', () => ({
  default: { get: mockGet, put: mockPut },
}))

import {
  addModel,
  addProvider,
  deleteProvider,
  getLLMConfig,
  saveDefaults,
  updateModel,
  updateProviderConfig,
  type LLMDefaults,
  type ModelConfig,
} from '@/services/api/config'

const FILE_URL = '/api/v1/plugins/llm_service/config/llm'
const ENV_URL = '/api/v1/config/env'

function fileResponse(data: Record<string, unknown>, etag = 'etag-1') {
  return { data: { plugin_id: 'llm_service', file_id: 'llm', label: 'llm', path: 'x', data, etag }, headers: {} }
}

function baseFileData() {
  return {
    models: {
      'minimax-m3': { provider: 'minimax', model_name: 'MiniMax-M3', display_name: 'MiniMax-M3' },
    },
    providers: {
      deepseek: {
        type: 'openai',
        api_base: 'https://api.deepseek.com/v1',
        keys: [{ id: 'deepseek_main', api_key: '${DEEPSEEK_API_KEY}' }],
      },
    },
    defaults: { chat: 'minimax-m3', embedding: 'bge-m3', tiers: { large: 'minimax-m3' } },
  }
}

beforeEach(() => {
  mockGet.mockReset()
  mockPut.mockReset()
  mockPut.mockResolvedValue({ data: { etag: 'etag-new' }, headers: {} })
  mockGet.mockImplementation(async (url: string) => {
    if (url === ENV_URL) return { data: { vars: { DEEPSEEK_API_KEY: '***' } } }
    throw new Error(`unexpected GET: ${url}`)
  })
})

describe('getLLMConfig（内核文件视图 + key 状态派生）', () => {
  it('映射 models/providers/defaults，${VAR} 占位按 .env 掩码视图判 has_key', async () => {
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(baseFileData())
      if (url === ENV_URL) return { data: { vars: { DEEPSEEK_API_KEY: '***' } } }
      throw new Error(`unexpected GET: ${url}`)
    })

    const config = await getLLMConfig()

    expect(config.defaults.chat).toBe('minimax-m3')
    expect(config.providers.deepseek.has_key).toBe(true)
    expect(config.providers.deepseek.env_var).toBe('DEEPSEEK_API_KEY')
  })

  it('占位符变量不在 .env → 未配置；磁盘明文（掩码视图 ****）→ 已配置', async () => {
    const data = baseFileData()
    ;(
      data.providers as Record<string, { keys: Array<{ id: string; api_key: string }> }>
    ).minimax = {
      type: 'openai',
      keys: [{ id: 'minimax_main', api_key: '****' }],
    }
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      if (url === ENV_URL) return { data: { vars: {} } }
      throw new Error(`unexpected GET: ${url}`)
    })

    const config = await getLLMConfig()

    expect(config.providers.deepseek.has_key).toBe(false)
    expect(config.providers.minimax.has_key).toBe(true)
    expect(config.providers.minimax.env_var).toBeNull()
  })
})

describe('saveDefaults（读-改-写整文件）', () => {
  it('patch 合并进 defaults，PUT 携带 If-Match 与全量内容', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data, 'etag-7')
      throw new Error(`unexpected GET: ${url}`)
    })
    const patch: Partial<LLMDefaults> = {
      chat: 'glm-5.2',
      tiers: { large: 'glm-5.2' },
    }

    const result = await saveDefaults(patch)

    expect(result).toEqual({ chat: 'glm-5.2', embedding: 'bge-m3', tiers: { large: 'glm-5.2' } })
    expect(mockPut).toHaveBeenCalledTimes(1)
    const [url, body] = mockPut.mock.calls[0]
    expect(url).toBe(FILE_URL)
    expect(body.if_match).toBe('etag-7')
    expect(body.data.defaults.chat).toBe('glm-5.2')
    // 未触碰的段原样保留（models/providers 不丢）
    expect(body.data.models['minimax-m3']).toBeDefined()
  })
})

describe('addModel（冲突派生规则）', () => {
  const newModel: ModelConfig = {
    provider: 'deepseek',
    model_name: 'MiniMax-M3',
    display_name: 'MiniMax-M3',
  }

  it('跨 provider 同名 → 派生 <id>-<provider>，绝不覆盖既有条目', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      throw new Error(`unexpected GET: ${url}`)
    })

    const { models, added_ids } = await addModel('minimax-m3', newModel)

    expect(added_ids).toEqual(['minimax-m3-deepseek'])
    expect(models['minimax-m3'].provider).toBe('minimax', '既有条目不被覆盖')
    expect(models['minimax-m3-deepseek']).toBeDefined()
  })

  it('同 provider 同 model_name → 真重复报错', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      throw new Error(`unexpected GET: ${url}`)
    })

    await expect(
      addModel('minimax-m3', {
        provider: 'minimax',
        model_name: 'MiniMax-M3',
        display_name: 'dup',
      }),
    ).rejects.toThrow('已存在')
  })

  it('唯一 id → 直增', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      throw new Error(`unexpected GET: ${url}`)
    })

    const { added_ids } = await addModel('glm-5.2', {
      provider: 'zhipu',
      model_name: 'glm-5.2',
      display_name: 'GLM',
    })
    expect(added_ids).toEqual(['glm-5.2'])
  })
})

describe('updateModel / updateProviderConfig', () => {
  it('updateModel 合并 partial；模型缺失报错', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      throw new Error(`unexpected GET: ${url}`)
    })

    const models = await updateModel('minimax-m3', { context_window: 128000 })
    expect(models['minimax-m3'].context_window).toBe(128000)
    expect(models['minimax-m3'].provider).toBe('minimax', '未提交字段保留')

    await expect(updateModel('no-such', { context_window: 1 })).rejects.toThrow('不存在')
  })

  it('明文 key → 写 /api/v1/config/env + yaml 占位符；keys 合并保留未提交字段', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      if (url === ENV_URL) return { data: { vars: {} } }
      throw new Error(`unexpected GET: ${url}`)
    })
    mockPut.mockResolvedValue({ data: {} })

    await updateProviderConfig('deepseek', {
      keys: [{ id: 'deepseek_main', api_key: 'sk-plaintext' }],
    })

    expect(mockPut).toHaveBeenCalledTimes(2)
    const [envUrl, envBody] = mockPut.mock.calls[0]
    expect(envUrl).toBe(ENV_URL)
    expect(envBody).toEqual({ set: { DEEPSEEK_API_KEY: 'sk-plaintext' } })
    const [fileUrl, fileBody] = mockPut.mock.calls[1]
    expect(fileUrl).toBe(FILE_URL)
    const keys = fileBody.data.providers.deepseek.keys
    expect(keys[0].api_key).toBe('${DEEPSEEK_API_KEY}', 'yaml 保持占位符')
  })

  it('掩码/示例值回传被剔除，不污染 yaml 也不写 .env', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      if (url === ENV_URL) return { data: { vars: {} } }
      throw new Error(`unexpected GET: ${url}`)
    })

    await updateProviderConfig('deepseek', {
      keys: [{ id: 'deepseek_main', api_key: '****' }],
    })

    const envWrite = mockPut.mock.calls.find(([url]) => url === ENV_URL)
    expect(envWrite).toBeUndefined()
    const keys = mockPut.mock.calls.find(([url]) => url === FILE_URL)?.[1].data.providers.deepseek.keys
    expect(keys[0].api_key).toBe('${DEEPSEEK_API_KEY}', '占位符保留磁盘值')
  })
})

describe('addProvider / deleteProvider', () => {
  it('添加：明文 key 入 .env、yaml 占位；重复报错', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      if (url === ENV_URL) return { data: { vars: {} } }
      throw new Error(`unexpected GET: ${url}`)
    })

    await addProvider('zhipu', { type: 'openai', api_key: 'sk-zhipu' })
    const [, body] = mockPut.mock.calls.find(([url]) => url === FILE_URL)
    expect(body.data.providers.zhipu.keys[0].api_key).toBe('${ZHIPU_API_KEY}')

    await expect(addProvider('deepseek', { type: 'openai' })).rejects.toThrow('已存在')
  })

  it('删除：树编辑后整文件 PUT', async () => {
    const data = baseFileData()
    mockGet.mockImplementation(async (url: string) => {
      if (url === FILE_URL) return fileResponse(data)
      throw new Error(`unexpected GET: ${url}`)
    })

    const providers = await deleteProvider('deepseek')
    expect(providers.deepseek).toBeUndefined()
    expect(mockPut).toHaveBeenCalledWith(
      FILE_URL,
      expect.objectContaining({ if_match: 'etag-1' }),
    )
  })
})
