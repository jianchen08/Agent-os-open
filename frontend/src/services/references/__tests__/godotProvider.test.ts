// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** godot 引用 provider 补测：有选中节点时 getSelection/getRow 的翻译形状 */
import { describe, expect, it, vi } from 'vitest'

const { mockGet, mockClear, mockInit, mockSubscribe, mockPreviewUrl } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockClear: vi.fn().mockResolvedValue(undefined),
  mockInit: vi.fn().mockResolvedValue(undefined),
  mockSubscribe: vi.fn().mockReturnValue(() => {}),
  mockPreviewUrl: vi.fn().mockReturnValue('blob:preview'),
}))

vi.mock('@/services/godot/selectionBridge', () => ({
  getGodotSelection: () => mockGet(),
  clearGodotSelection: (...a: unknown[]) => mockClear(...a),
  godotPreviewUrl: (...a: unknown[]) => mockPreviewUrl(...a),
  initGodotSelection: (...a: unknown[]) => mockInit(...a),
  subscribeGodotSelection: (...a: unknown[]) => mockSubscribe(...a),
}))

import { getReferenceProviders } from '@/services/references/referenceProviders'
import '../godotProvider'

const FULL = {
  connected: true,
  signature: 'sig-1',
  scene: { path: '/res/demo.tscn' },
  items: [
    {
      name: 'Player',
      type: 'CharacterBody2D',
      path: '/root/Player',
      position: '(24, 80)',
      preview_kind: 'viewport',
    },
  ],
}

describe('godot reference provider', () => {
  it('注册为 source=godot 的 provider', () => {
    expect(getReferenceProviders().some((p) => p.source === 'godot')).toBe(true)
  })

  it('有选中节点 → getSelection 翻译为通用契约（scene 属性 + 节点映射 + position extra）', async () => {
    mockGet.mockReturnValue(FULL)
    const provider = getReferenceProviders().find((p) => p.source === 'godot')!
    const sel = provider.getSelection!()!
    expect(sel.source).toBe('godot')
    expect(sel.attrs).toEqual({ scene: '/res/demo.tscn' })
    expect(sel.items[0]).toMatchObject({
      name: 'Player',
      type: 'CharacterBody2D',
      path: '/root/Player',
      extra: 'position=(24, 80)',
    })
    await provider.consume!('t1')
    expect(mockClear).toHaveBeenCalled()
  })

  it('getRow：chips 带 previewUrl 与副标题；activateRow 触发 initGodotSelection', async () => {
    mockGet.mockReturnValue(FULL)
    const provider = getReferenceProviders().find((p) => p.source === 'godot')!
    const row = provider.getRow!()!
    expect(row.label).toBe('Godot 引用')
    expect(row.connected).toBe(true)
    expect(row.chips[0].previewUrl).toBe('blob:preview')
    expect(row.chips[0].subtitle).toContain('/root/Player · (24, 80)')

    await provider.activateRow!('thread-9')
    expect(mockInit).toHaveBeenCalledWith('thread-9')
  })

  it('空选中 → getSelection/getRow 均 null', () => {
    mockGet.mockReturnValue({ connected: false, items: [], signature: '' })
    const provider = getReferenceProviders().find((p) => p.source === 'godot')!
    expect(provider.getSelection!()).toBeNull()
    expect(provider.getRow!()).toBeNull()
  })
})
