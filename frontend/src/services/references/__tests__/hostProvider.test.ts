// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** host 引用 provider 补测：有选中节点时 getSelection/getRow 的翻译形状（source 动态透传） */
import { describe, expect, it, vi } from 'vitest'

const { mockGet, mockClear, mockInit, mockSubscribe, mockPreviewUrl } = vi.hoisted(() => ({
  mockGet: vi.fn(),
  mockClear: vi.fn().mockResolvedValue(undefined),
  mockInit: vi.fn().mockResolvedValue(undefined),
  mockSubscribe: vi.fn().mockReturnValue(() => {}),
  mockPreviewUrl: vi.fn().mockReturnValue('blob:preview'),
}))

vi.mock('@/services/host/hostBridge', () => ({
  getHostSelection: () => mockGet(),
  clearHostSelection: (...a: unknown[]) => mockClear(...a),
  hostPreviewUrl: (...a: unknown[]) => mockPreviewUrl(...a),
  initHostSelection: (...a: unknown[]) => mockInit(...a),
  subscribeHostSelection: (...a: unknown[]) => mockSubscribe(...a),
}))

import { getReferenceProviders } from '@/services/references/referenceProviders'
import '../hostProvider'

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

describe('host reference provider', () => {
  it('注册为槽位 source=host 的 provider（注册键是槽位标识，非消息块 source）', () => {
    expect(getReferenceProviders().some((p) => p.source === 'host')).toBe(true)
  })

  it('有选中节点 → getSelection 翻译为通用契约（scene 属性 + 节点映射 + position extra）', async () => {
    mockGet.mockReturnValue(FULL)
    const provider = getReferenceProviders().find((p) => p.source === 'host')!
    const sel = provider.getSelection!()!
    expect(sel.source).toBe('host')
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

  it('快照携带配置的连接者身份 → source/标签/kind 动态透传（不烧死宿主名）', async () => {
    mockGet.mockReturnValue({ ...FULL, source: 'vscode', display_name: 'VSCode' })
    const provider = getReferenceProviders().find((p) => p.source === 'host')!
    const sel = provider.getSelection!()!
    expect(sel.source).toBe('vscode')
    const row = provider.getRow!()!
    expect(row.label).toBe('VSCode 引用')
    expect(row.chips[0].kind).toBe('vscode-node')

    await provider.activateRow!('thread-9')
    expect(mockInit).toHaveBeenCalledWith('thread-9')
  })

  it('快照无身份字段 → 回退槽位缺省 source=host', () => {
    mockGet.mockReturnValue(FULL)
    const provider = getReferenceProviders().find((p) => p.source === 'host')!
    expect(provider.getSelection!()!.source).toBe('host')
    expect(provider.getRow!()!.label).toBe('host 引用')
  })

  it('空选中 → getSelection/getRow 均 null', () => {
    mockGet.mockReturnValue({ connected: false, items: [], signature: '' })
    const provider = getReferenceProviders().find((p) => p.source === 'host')!
    expect(provider.getSelection!()).toBeNull()
    expect(provider.getRow!()).toBeNull()
  })
})
