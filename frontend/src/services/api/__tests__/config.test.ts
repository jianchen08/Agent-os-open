// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 配置管理 API 服务测试（LLM 配置域，内核单一配置面）
 *
 * 批次 A1 后读写同源：llm.yaml 经 PLUGIN_CONFIG.FILE
 * （/api/v1/plugins/llm_service/config/llm，GET 掩码视图 + ETag → PUT
 * {data, if_match} 乐观锁）读写；provider key 状态派生消费
 * PLUGIN_CONFIG.USER_ENV（/api/v1/config/env）掩码视图；明文 key 提取
 * 写用户 .env。断言请求 URL/载荷与响应解包，以及读-改-写树的编辑语义。
 */

/* eslint-disable import-x/order */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as configApi from '@/services/api/config'

vi.mock('../client', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
  },
}))

import apiClient from '@/services/api/client'

const LLM_FILE_URL = '/api/v1/plugins/llm_service/config/llm'
const USER_ENV_URL = '/api/v1/config/env'

/** axios 形态响应（headers 供 ETag 提取，缺省即 TypeError） */
const okResponse = (data: unknown, etag?: string) => ({
  data: { data, ...(etag ? { etag } : {}) },
  headers: {},
})

/** CONFIG.* 平铺端点响应（body 即数据，无文件信封） */
const flatResponse = (data: unknown) => ({ data })

describe('配置管理 API（LLM 配置域）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.clearAllMocks()
  })

  describe('getLLMConfig - 获取 LLM 配置（文件视图 + key 状态派生）', () => {
    it('GET 配置文件 + .env 掩码视图，占位/掩码/空三形态 key 派生正确', async () => {
      const yaml = {
        models: { m1: { provider: 'openai', model_name: 'gpt-4o' } },
        providers: {
          // ${VAR} 占位：has_key 按 .env 掩码视图判定
          deepseek: { type: 'deepseek', keys: [{ id: 'k', api_key: '${DEEPSEEK_API_KEY}' }] },
          // 磁盘明文 key 的内核掩码值：视为已配置、无环境变量
          zai: { type: 'zai', keys: [{ id: 'k', api_key: '****' }] },
          // 空 key：未配置
          foo: { type: 'openai', keys: [] },
        },
        defaults: { chat: 'm1', tiers: {}, embedding: 'e1' },
      }
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(okResponse(yaml, 'v1'))
        .mockResolvedValueOnce({ data: { vars: { DEEPSEEK_API_KEY: '***' } }, headers: {} })

      const result = await configApi.getLLMConfig()

      expect(apiClient.get).toHaveBeenNthCalledWith(1, LLM_FILE_URL)
      expect(apiClient.get).toHaveBeenNthCalledWith(2, USER_ENV_URL)
      expect(result.defaults.chat).toBe('m1')
      expect(result.models.m1.provider).toBe('openai')
      // 三形态 key 派生（占位命中 env / 掩码视为配置 / 空未配置）
      expect(result.providers.deepseek).toMatchObject({ has_key: true, env_var: 'DEEPSEEK_API_KEY' })
      expect(result.providers.zai).toMatchObject({ has_key: true, env_var: null })
      expect(result.providers.foo).toMatchObject({ has_key: false, env_var: null })
    })

    it('启用重试时失败后重试成功（整闭包重跑：文件 GET 两次 + env GET 一次）', async () => {
      const yaml = { models: {}, providers: {}, defaults: { chat: 'm1', tiers: {}, embedding: 'e1' } }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce(okResponse(yaml, 'v1'))
        .mockResolvedValueOnce({ data: { vars: {} }, headers: {} })

      const result = await configApi.getLLMConfig({ retry: true, maxRetries: 2, retryDelay: 1 })

      expect(result.defaults.chat).toBe('m1')
      expect(apiClient.get).toHaveBeenCalledTimes(3)
    })
  })

  describe('getProviderTypes - 提供者类型清单', () => {
    it('请求 provider-types 端点', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(flatResponse({ types: ['openai', 'deepseek'] }))

      const result = await configApi.getProviderTypes()

      expect(result.types).toContain('deepseek')
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/llm_service/config/llm/provider-types',
      )
    })
  })

  describe('getRemoteModels - 远端模型', () => {
    it('按 providerId 请求并解包', async () => {
      const resp = { provider: 'deepseek', models: [{ id: 'deepseek-chat', owned_by: 'deepseek' }] }
      vi.mocked(apiClient.get).mockResolvedValueOnce(flatResponse(resp))

      const result = await configApi.getRemoteModels('deepseek')

      expect(result.models[0].id).toBe('deepseek-chat')
      expect(apiClient.get).toHaveBeenCalledWith(
        '/ext/llm_service/config/llm/providers/deepseek/remote-models',
      )
    })
  })

  describe('getLLMPresets - 配置面预置声明', () => {
    it('请求 presets 端点并解包（插件下发形状原样透传）', async () => {
      const presets = {
        provider_groups: [
          { label: '国内', providers: [['deepseek', 'DeepSeek']] as [string, string][] },
        ],
        common_provider_types: ['openai', 'deepseek'],
        thinking_strength: { levels: ['off', 'low', 'high'], allowed_keys: ['thinking'] },
      }
      vi.mocked(apiClient.get).mockResolvedValueOnce(flatResponse(presets))

      const result = await configApi.getLLMPresets()

      expect(result).toEqual(presets)
      // 性质断言：[provider_id, 显示名] 二元组结构原样保留，不被二次包装
      expect(result.provider_groups[0].providers[0]).toHaveLength(2)
    })

    it('空声明（插件未下发分组/常用类型/白名单）→ 返回空形状而非抛错', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(
        flatResponse({
          provider_groups: [],
          common_provider_types: [],
          thinking_strength: { levels: [], allowed_keys: [] },
        }),
      )

      const result = await configApi.getLLMPresets()

      expect(result.provider_groups).toHaveLength(0)
      expect(result.common_provider_types).toHaveLength(0)
      expect(result.thinking_strength.levels).toHaveLength(0)
    })

    it('透传重试选项：失败后按 maxRetries 重试成功', async () => {
      const presets = {
        provider_groups: [],
        common_provider_types: [],
        thinking_strength: { levels: ['off'], allowed_keys: [] },
      }
      vi.mocked(apiClient.get)
        .mockRejectedValueOnce(new Error('Network Error'))
        .mockResolvedValueOnce(flatResponse(presets))

      const result = await configApi.getLLMPresets({ retry: true, maxRetries: 2, retryDelay: 1 })

      expect(result.thinking_strength.levels).toEqual(['off'])
    })
  })

  describe('getModels / getDefaults（同一文件视图的投影）', () => {
    it('getModels 解包 models', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(okResponse({ models: { m1: {} } }, 'v1'))
        .mockResolvedValueOnce({ data: { vars: {} }, headers: {} })

      const result = await configApi.getModels()

      expect(result.models).toEqual({ m1: {} })
      expect(apiClient.get).toHaveBeenCalledWith(LLM_FILE_URL)
    })

    it('getDefaults 解包 defaults', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ defaults: { chat: 'm1', tiers: { fast: 'm2' }, embedding: 'e1' } }, 'v1'),
        )
        .mockResolvedValueOnce({ data: { vars: {} }, headers: {} })

      const result = await configApi.getDefaults()

      expect(result.tiers.fast).toBe('m2')
    })
  })

  describe('模型 CRUD（读-改-写 llm.yaml）', () => {
    it('addModel 新 id：写入 models，PUT 整树 + if_match，返回 added_ids', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ models: {} }, 'v1'))
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const config = { provider: 'openai', model_name: 'gpt-4o', display_name: 'GPT-4o' }
      const result = await configApi.addModel('gpt4o', config)

      expect(result.added_ids).toEqual(['gpt4o'])
      expect(result.models.gpt4o).toEqual(config)
      expect(apiClient.put).toHaveBeenCalledWith(LLM_FILE_URL, {
        data: { models: { gpt4o: config } },
        if_match: 'v1',
      })
    })

    it('addModel 同 id 不同 provider：派生 `<id>-<provider>` 不覆盖既有条目', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ models: { gpt4o: { provider: 'openai', model_name: 'name-x' } } }, 'v1'),
        )
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const config = { provider: 'deepseek', model_name: 'name-x' }
      const result = await configApi.addModel('gpt4o', config)

      expect(result.added_ids).toEqual(['gpt4o-deepseek'])
      expect(result.models['gpt4o']).toEqual({ provider: 'openai', model_name: 'name-x' })
      expect(result.models['gpt4o-deepseek']).toEqual(config)
    })

    it('addModel 同 id 同 provider 同 model_name（真重复）：报错不写', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ models: { gpt4o: { provider: 'openai', model_name: 'name-x' } } }, 'v1'),
        )

      await expect(
        configApi.addModel('gpt4o', { provider: 'openai', model_name: 'name-x' }),
      ).rejects.toThrow("模型 'gpt4o' 已存在")
      expect(apiClient.put).not.toHaveBeenCalled()
    })

    it('updateModel 合并到既有条目后 PUT 整树', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ models: { gpt4o: { provider: 'openai', model_name: 'gpt-4o' } } }, 'v1'),
        )
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.updateModel('gpt4o', { display_name: 'GPT-4o 新' })

      expect(result.gpt4o).toEqual({
        provider: 'openai',
        model_name: 'gpt-4o',
        display_name: 'GPT-4o 新',
      })
      expect(apiClient.put).toHaveBeenCalledWith(LLM_FILE_URL, {
        data: {
          models: { gpt4o: { provider: 'openai', model_name: 'gpt-4o', display_name: 'GPT-4o 新' } },
        },
        if_match: 'v1',
      })
    })

    it('updateModel 不存在：报错不 PUT', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ models: {} }, 'v1'))

      await expect(configApi.updateModel('ghost', {})).rejects.toThrow("模型 'ghost' 不存在")
      expect(apiClient.put).not.toHaveBeenCalled()
    })

    it('deleteModel 从树中剔除后 PUT', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(okResponse({ models: { gpt4o: {}, other: {} } }, 'v1'))
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.deleteModel('gpt4o')

      expect(result).toEqual({ other: {} })
      expect(apiClient.put).toHaveBeenCalledWith(LLM_FILE_URL, {
        data: { models: { other: {} } },
        if_match: 'v1',
      })
    })
  })

  describe('提供者 CRUD（明文 key 提取 .env + keys 索引合并）', () => {
    it('updateProviderConfig 明文 api_key：写 .env、yaml 留占位、PUT 整树', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse(
            { providers: { deepseek: { type: 'deepseek', keys: [{ id: 'deepseek_main', api_key: '***' }] } } },
            'v1',
          ),
        )
      vi.mocked(apiClient.put).mockResolvedValue({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.updateProviderConfig('deepseek', { api_key: 'sk-x' })

      // 先写用户 .env，再 PUT 配置树
      expect(apiClient.put).toHaveBeenNthCalledWith(1, USER_ENV_URL, {
        set: { DEEPSEEK_API_KEY: 'sk-x' },
      })
      expect(apiClient.put).toHaveBeenNthCalledWith(2, LLM_FILE_URL, {
        data: {
          providers: {
            deepseek: {
              type: 'deepseek',
              keys: [{ id: 'deepseek_main', api_key: '${DEEPSEEK_API_KEY}' }],
            },
          },
        },
        if_match: 'v1',
      })
      expect(result.deepseek).toBeTruthy()
    })

    it('updateProviderConfig 不带 key：keys 磁盘值保留（占位符不清掉）', async () => {
      const diskKeys = [{ id: 'deepseek_main', api_key: '${DEEPSEEK_API_KEY}', rpm: 60 }]
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ providers: { deepseek: { type: 'deepseek', keys: diskKeys } } }, 'v1'),
        )
      vi.mocked(apiClient.put).mockResolvedValue({ data: { etag: 'v2' }, headers: {} })

      await configApi.updateProviderConfig('deepseek', { api_base: 'https://x' })

      expect(apiClient.put).not.toHaveBeenCalledWith(USER_ENV_URL, expect.anything())
      const [, filePayload] = vi.mocked(apiClient.put).mock.calls[0]
      expect(filePayload.data.providers.deepseek.keys[0].api_key).toBe('${DEEPSEEK_API_KEY}')
      expect(filePayload.data.providers.deepseek.api_base).toBe('https://x')
    })

    it('updateProviderConfig 不存在：报错不 PUT', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ providers: {} }, 'v1'))

      await expect(configApi.updateProviderConfig('ghost', {})).rejects.toThrow("提供商 'ghost' 不存在")
      expect(apiClient.put).not.toHaveBeenCalled()
    })

    it('addProvider 顶层明文 api_key：迁移为 keys[0] 占位 + 写 .env', async () => {
      vi.mocked(apiClient.get).mockResolvedValueOnce(okResponse({ providers: {} }, 'v1'))
      vi.mocked(apiClient.put).mockResolvedValue({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.addProvider('deepseek', { type: 'deepseek', api_key: 'sk-x' })

      expect(apiClient.put).toHaveBeenNthCalledWith(1, USER_ENV_URL, {
        set: { DEEPSEEK_API_KEY: 'sk-x' },
      })
      const [, filePayload] = vi.mocked(apiClient.put).mock.calls[1]
      expect(filePayload.data.providers.deepseek).toEqual({
        type: 'deepseek',
        keys: [{ id: 'deepseek_main', api_key: '${DEEPSEEK_API_KEY}' }],
      })
      expect(result.deepseek).toBeTruthy()
    })

    it('deleteProvider 从树中剔除后 PUT', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ providers: { deepseek: { type: 'deepseek', keys: [] }, zai: { type: 'zai', keys: [] } } }, 'v1'),
        )
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.deleteProvider('deepseek')

      expect(result.zai).toBeTruthy()
      expect(result.deepseek).toBeUndefined()
      const [, filePayload] = vi.mocked(apiClient.put).mock.calls[0]
      expect(Object.keys(filePayload.data.providers)).toEqual(['zai'])
    })
  })

  describe('saveDefaults - 更新默认模型配置（合并磁盘值）', () => {
    it('chat+tiers 补丁合并进磁盘 defaults 后 PUT，返回合并结果', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(
          okResponse({ defaults: { chat: 'm1', tiers: { fast: 'm0' }, embedding: 'e1' } }, 'v1'),
        )
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.saveDefaults({ chat: 'gpt-4o', tiers: { fast: 'gpt-4o-mini' } })

      expect(result).toEqual({ chat: 'gpt-4o', tiers: { fast: 'gpt-4o-mini' }, embedding: 'e1' })
      expect(apiClient.put).toHaveBeenCalledWith(LLM_FILE_URL, {
        data: {
          defaults: { chat: 'gpt-4o', tiers: { fast: 'gpt-4o-mini' }, embedding: 'e1' },
        },
        if_match: 'v1',
      })
    })

    it('磁盘 defaults 无 tiers/embedding：仅更新 chat 不发明默认值', async () => {
      vi.mocked(apiClient.get)
        .mockResolvedValueOnce(okResponse({ defaults: { chat: 'm1' } }, 'v1'))
      vi.mocked(apiClient.put).mockResolvedValueOnce({ data: { etag: 'v2' }, headers: {} })

      const result = await configApi.saveDefaults({ chat: 'm2' })

      const [, filePayload] = vi.mocked(apiClient.put).mock.calls[0]
      expect(filePayload.data.defaults).toEqual({ chat: 'm2' })
      expect(filePayload.data.defaults).not.toHaveProperty('tiers')
      expect(filePayload.data.defaults).not.toHaveProperty('embedding')
      expect(result.chat).toBe('m2')
    })
  })
})
