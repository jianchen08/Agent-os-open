// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * useModelContextInfo 单测：模型 context_window 查询与有效性判定
 */
import { renderHook, waitFor } from '@testing-library/react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
const getModelsMock = vi.fn()
vi.mock('@/services/api/config', () => ({
  getModels: (...args: unknown[]) => getModelsMock(...args),
}))
import { useModelContextInfo } from '../useModelContextInfo'

describe('useModelContextInfo', () => {
  beforeEach(() => {
    getModelsMock.mockReset()
  })

  it('模型有效：返回 context_window 与 isValid=true', async () => {
    getModelsMock.mockResolvedValue({
      models: { 'gpt-4o': { context_window: 128000 } },
    })

    const { result } = renderHook(() => useModelContextInfo('gpt-4o'))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(128000)
    expect(result.current.isValid).toBe(true)
  })

  it('模型未配置：contextWindow=0、isValid=false（不冒充真实模型）', async () => {
    getModelsMock.mockResolvedValue({ models: { 'gpt-4o': { context_window: 128000 } } })

    const { result } = renderHook(() => useModelContextInfo('unknown-model'))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(0)
    expect(result.current.isValid).toBe(false)
  })

  it('modelName 为空：contextWindow=0、isValid=false（挂载仍拉取一次模型配置）', async () => {
    getModelsMock.mockResolvedValue({ models: { 'gpt-4o': { context_window: 128000 } } })

    const { result } = renderHook(() => useModelContextInfo(undefined))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(0)
    expect(result.current.isValid).toBe(false)
    // 现状契约：模型配置在挂载时无条件拉取一次（与 modelName 无关）
    expect(getModelsMock).toHaveBeenCalledTimes(1)
  })

  it('context_window 为 0 的模型视为无效', async () => {
    getModelsMock.mockResolvedValue({ models: { 'm1': { context_window: 0 } } })

    const { result } = renderHook(() => useModelContextInfo('m1'))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(0)
    expect(result.current.isValid).toBe(false)
  })

  it('加载失败：进 error 态（可区分「接口失败」与「配置无此模型」）', async () => {
    getModelsMock.mockRejectedValue(new Error('fetch failed'))

    const { result } = renderHook(() => useModelContextInfo('gpt-4o'))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(0)
    expect(result.current.isValid).toBe(false)
    // S4 收口：失败必须置 error 态，不再与「未知模型」同表现
    expect(result.current.error).toContain('fetch failed')
  })

  it('配置成功但无此模型：error=null（与失败态互斥，语义可区分）', async () => {
    getModelsMock.mockResolvedValue({ models: {} })

    const { result } = renderHook(() => useModelContextInfo('no-such-model'))
    await waitFor(() => expect(result.current.isLoading).toBe(false))

    expect(result.current.isValid).toBe(false)
    expect(result.current.error).toBeNull()
  })

  it('刷新成功后清除上一次的 error 态', async () => {
    getModelsMock.mockRejectedValueOnce(new Error('boom'))
    getModelsMock.mockResolvedValueOnce({ models: { 'gpt-4o': { context_window: 9000 } } })

    const { result } = renderHook(() => useModelContextInfo('gpt-4o'))
    await waitFor(() => expect(result.current.error).toContain('boom'))

    result.current.refresh()
    await waitFor(() => expect(result.current.contextWindow).toBe(9000))
    expect(result.current.error).toBeNull()
  })

  it('refresh 手动触发重新拉取', async () => {
    getModelsMock.mockResolvedValueOnce({ models: {} })
    getModelsMock.mockResolvedValueOnce({ models: { 'gpt-4o': { context_window: 200000 } } })

    const { result } = renderHook(() => useModelContextInfo('gpt-4o'))

    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(0)

    result.current.refresh()
    await waitFor(() => expect(result.current.contextWindow).toBe(200000))
    expect(getModelsMock).toHaveBeenCalledTimes(2)
  })
})

describe('useModelContextInfo — 模型名三级匹配（显示名 vs 配置键）', () => {
  /** 现实形态：配置键小写，条目 model_name 才是真实模型名（管道 state 行 llm_model 来源于此） */
  const REGISTRY = {
    'minimax-m3': { model_name: 'MiniMax-M3', context_window: 196608 },
    'deepseek-v3': { model_name: 'DeepSeek-V3', context_window: 128000 },
    'alias-entry': { model_name: 'qwen-max', context_window: 32768 },
    'kimi-entry': { model_name: 'kimi-k2', context_window: 131072 },
  }

  const queryWith = (modelName: string) => {
    getModelsMock.mockResolvedValue({ models: REGISTRY })
    const { result } = renderHook(() => useModelContextInfo(modelName))
    return result
  }

  it('精确 id 命中：查询与配置键完全一致（回归保护）', async () => {
    const result = queryWith('deepseek-v3')
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(128000)
    expect(result.current.isValid).toBe(true)
  })

  it('键忽略大小写命中：显示名 MiniMax-M3 → 配置键 minimax-m3', async () => {
    const result = queryWith('MiniMax-M3')
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(196608)
    expect(result.current.isValid).toBe(true)
  })

  it('键忽略大小写命中（第二组）：DeepSeek-V3 → deepseek-v3', async () => {
    const result = queryWith('DeepSeek-V3')
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(128000)
    expect(result.current.isValid).toBe(true)
  })

  it('键不匹配但 model_name 字段命中：真名对真名', async () => {
    const result = queryWith('qwen-max')
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(32768)
    expect(result.current.isValid).toBe(true)
  })

  it('model_name 字段命中（第二组）：kimi-k2', async () => {
    const result = queryWith('kimi-k2')
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(131072)
    expect(result.current.isValid).toBe(true)
  })

  it('三级全不中：contextWindow=0、isValid=false、error=null（配置里确实没有）', async () => {
    const result = queryWith('no-such-model')
    await waitFor(() => expect(result.current.isLoading).toBe(false))
    expect(result.current.contextWindow).toBe(0)
    expect(result.current.isValid).toBe(false)
    expect(result.current.error).toBeNull()
  })
})
