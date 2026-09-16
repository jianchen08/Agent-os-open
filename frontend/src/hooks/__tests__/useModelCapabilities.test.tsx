// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * useModelCapabilities 单测：多模态能力获取、缓存、输入能力计算
 */
import { render, renderHook, waitFor } from '@testing-library/react'
import { StrictMode } from 'react'
import { describe, it, expect, beforeEach, vi } from 'vitest'
const getModelCapabilitiesMock = vi.fn()
vi.mock('@/services/api/files', () => ({
  getModelCapabilities: (...args: unknown[]) => getModelCapabilitiesMock(...args),
}))
import { useModelCapabilities, clearCapabilitiesCache } from '../useModelCapabilities'

const fullCapabilities = {
  model_name: 'gpt-4o',
  supports_image: true,
  supported_image_types: ['image/png', 'image/jpeg'],
  max_image_size: 10485760,
  supports_audio: true,
  supported_audio_types: ['audio/mpeg'],
  max_audio_size: 5242880,
  supports_video: false,
  supported_video_types: [],
  max_video_size: 0,
  is_multimodal: true,
}

describe('useModelCapabilities', () => {
  beforeEach(() => {
    getModelCapabilitiesMock.mockReset()
    clearCapabilitiesCache()
  })

  it('多模态模型：能力转换 + 输入能力按能力开启', async () => {
    getModelCapabilitiesMock.mockResolvedValue(fullCapabilities)

    const { result } = renderHook(() => useModelCapabilities('gpt-4o'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.capabilities).toMatchObject({
      modelName: 'gpt-4o',
      supportsImage: true,
      supportsAudio: true,
      supportsVideo: false,
      isMultimodal: true,
    })
    expect(result.current.inputCapabilities).toMatchObject({
      showAttachmentButton: true,
      showImageUpload: true,
      showAudioUpload: true,
      showVideoUpload: false,
      canPasteImage: true,
      canDragDrop: true,
      acceptedFileTypes: 'image/png,image/jpeg,audio/mpeg',
      capabilityTags: ['图片', '音频'],
    })
    expect(getModelCapabilitiesMock).toHaveBeenCalledWith('gpt-4o')
  })

  it('无多模态模型：默认输入能力（附件可用、多模态全关）', async () => {
    getModelCapabilitiesMock.mockResolvedValue({
      model_name: 'text-only',
      supports_image: false,
      supported_image_types: [],
      max_image_size: 0,
      supports_audio: false,
      supported_audio_types: [],
      max_audio_size: 0,
      supports_video: false,
      supported_video_types: [],
      max_video_size: 0,
      is_multimodal: false,
    })

    const { result } = renderHook(() => useModelCapabilities('text-only'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.inputCapabilities).toEqual({
      showAttachmentButton: true,
      showImageUpload: false,
      showAudioUpload: false,
      showVideoUpload: false,
      canPasteImage: false,
      canDragDrop: true,
      acceptedFileTypes: '',
      capabilityTags: [],
    })
  })

  it('请求失败：静默回退默认能力（capabilities=null）', async () => {
    getModelCapabilitiesMock.mockRejectedValue(new Error('boom'))

    const { result } = renderHook(() => useModelCapabilities('gpt-4o'))

    await waitFor(() => expect(result.current.loading).toBe(false))
    expect(result.current.capabilities).toBeNull()
    expect(result.current.inputCapabilities.showImageUpload).toBe(false)
  })

  it('无效模型名（空/unknown）：清空能力且不发请求', () => {
    const { result } = renderHook(() => useModelCapabilities('unknown'))

    expect(result.current.capabilities).toBeNull()
    expect(result.current.inputCapabilities).toEqual(
      expect.objectContaining({ showImageUpload: false }),
    )
    expect(getModelCapabilitiesMock).not.toHaveBeenCalled()
  })

  it('相同模型名不重复请求（缓存命中）', async () => {
    getModelCapabilitiesMock.mockResolvedValue(fullCapabilities)

    const { rerender, result } = renderHook(({ name }) => useModelCapabilities(name), {
      initialProps: { name: 'gpt-4o' },
    })

    await waitFor(() => expect(result.current.capabilities?.modelName).toBe('gpt-4o'))
    rerender({ name: 'gpt-4o' })
    rerender({ name: 'gpt-4o' })

    expect(getModelCapabilitiesMock).toHaveBeenCalledTimes(1)
  })

  it('切换模型名：重新请求并更新能力', async () => {
    getModelCapabilitiesMock.mockResolvedValueOnce(fullCapabilities)
    getModelCapabilitiesMock.mockResolvedValueOnce({
      model_name: 'other',
      supports_image: false,
      supported_image_types: [],
      max_image_size: 0,
      supports_audio: false,
      supported_audio_types: [],
      max_audio_size: 0,
      supports_video: true,
      supported_video_types: ['video/mp4'],
      max_video_size: 0,
      is_multimodal: true,
    })

    const { rerender, result } = renderHook(({ name }) => useModelCapabilities(name), {
      initialProps: { name: 'gpt-4o' },
    })

    await waitFor(() => expect(result.current.capabilities?.modelName).toBe('gpt-4o'))
    rerender({ name: 'other' })
    await waitFor(() => expect(result.current.capabilities?.modelName).toBe('other'))
    expect(result.current.inputCapabilities.showVideoUpload).toBe(true)
    expect(getModelCapabilitiesMock).toHaveBeenCalledTimes(2)
  })

  it('切走再切回同一模型：命中模块级缓存，能力即时恢复且不重发请求', async () => {
    getModelCapabilitiesMock.mockResolvedValue(fullCapabilities)

    const { rerender, result } = renderHook(({ name }) => useModelCapabilities(name), {
      initialProps: { name: 'gpt-4o' },
    })
    await waitFor(() => expect(result.current.capabilities?.modelName).toBe('gpt-4o'))
    expect(getModelCapabilitiesMock).toHaveBeenCalledTimes(1)

    // 切到无效模型名：能力清空（该名不落缓存）
    rerender({ name: 'unknown' })
    await waitFor(() => expect(result.current.capabilities).toBeNull())

    // 切回：缓存命中短路——不 loading、不重发请求，能力面完整还原
    rerender({ name: 'gpt-4o' })
    await waitFor(() => expect(result.current.capabilities?.modelName).toBe('gpt-4o'))
    expect(result.current.loading).toBe(false)
    expect(getModelCapabilitiesMock).toHaveBeenCalledTimes(1)
    expect(result.current.inputCapabilities).toMatchObject({
      showImageUpload: true,
      showAudioUpload: true,
      acceptedFileTypes: 'image/png,image/jpeg,audio/mpeg',
    })
  })

  it('同一模型名重复执行 effect（StrictMode 双调用）→ 不重复请求', async () => {
    getModelCapabilitiesMock.mockResolvedValue(fullCapabilities)

    // 应用真实挂载形态（main.tsx 以 StrictMode 包裹）：effect 双调用必须早退
    const seen: Array<string | undefined> = []
    function Harness() {
      const caps = useModelCapabilities('gpt-4o')
      seen.push(caps.capabilities?.modelName)
      return <span data-testid="cap-name">{caps.capabilities?.modelName ?? 'none'}</span>
    }
    const { getByTestId } = render(
      <StrictMode>
        <Harness />
      </StrictMode>,
    )

    await waitFor(() => expect(getByTestId('cap-name').textContent).toBe('gpt-4o'))
    // 重入早退：第二次 effect 执行不落到请求分支（能力仍完整、请求只发一次）
    expect(getModelCapabilitiesMock).toHaveBeenCalledTimes(1)
    expect(seen.at(-1)).toBe('gpt-4o')
  })
})
