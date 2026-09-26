/** @feature FP-T12 皮肤 hooks 启用确认卡 | @ci: frontend-test */
/**
 * SkinConsentDialog 行为测试（2026-09-25 准入分级波2）
 *
 * 行为契约：
 * - store 无 pending → 不渲染
 * - pending 首启用 → 呈确认卡（来源插件/指纹可见）
 * - 确认 → saveSkinHookPin 落 pin + 重应用皮肤 + pending 清空
 * - 取消 → 仅清 pending（不落 pin）
 *
 * skinRuntime 整模块 mock（applyPluginSkin 是重 I/O；pin 落地行为由
 * skinRuntime.hookPin.test.ts 的真实实现测试覆盖，此处只验 Dialog 消费契约）。
 */

import { render, screen, fireEvent, waitFor } from '@testing-library/react'
import React from 'react'
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { useSkinConsentStore } from '@/stores/skinConsentStore'
import { SkinConsentDialog } from '../SkinConsentDialog'

vi.mock('@/services/skinRuntime', () => ({
  applyPluginSkin: vi.fn().mockResolvedValue(undefined),
  saveSkinHookPin: vi.fn(),
}))

import { applyPluginSkin, saveSkinHookPin } from '@/services/skinRuntime'

const theme = { pluginId: 'skin_plugin', skin: 's1', name: 'S1', base: 'dark' } as const

describe('SkinConsentDialog', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    useSkinConsentStore.getState().setPending(null)
  })

  it('无 pending → 不渲染', () => {
    render(<SkinConsentDialog />)
    expect(screen.queryByTestId('skin-consent-overlay')).toBeNull()
  })

  it('首启用 pending → 呈卡（来源插件 + 指纹）', () => {
    useSkinConsentStore.getState().setPending({
      theme: theme as unknown as Parameters<typeof applyPluginSkin>[0],
      scope: 'skin_plugin:s1',
      hash: 'a'.repeat(64),
      reason: 'first',
    })
    render(<SkinConsentDialog />)
    expect(screen.getByTestId('skin-consent-overlay')).toBeInTheDocument()
    expect(screen.getByText(/skin_plugin/)).toBeInTheDocument()
    expect(screen.getByText(/确认执行/)).toBeInTheDocument()
  })

  it('确认 → 落 pin + 重应用 + 清 pending', async () => {
    useSkinConsentStore.getState().setPending({
      theme: theme as unknown as Parameters<typeof applyPluginSkin>[0],
      scope: 'skin_plugin:s1',
      hash: 'a'.repeat(64),
      reason: 'first',
    })
    render(<SkinConsentDialog />)
    fireEvent.click(screen.getByTestId('skin-consent-confirm'))

    await waitFor(() => {
      expect(saveSkinHookPin).toHaveBeenCalledWith('skin_plugin:s1', 'a'.repeat(64))
      expect(applyPluginSkin).toHaveBeenCalledTimes(1)
      expect(useSkinConsentStore.getState().pending).toBeNull()
    })
  })

  it('取消 → 仅清 pending，不落 pin 不重应用（≥2 组区分输入）', () => {
    useSkinConsentStore.getState().setPending({
      theme: theme as unknown as Parameters<typeof applyPluginSkin>[0],
      scope: 'skin_plugin:s1',
      hash: 'a'.repeat(64),
      reason: 'drift',
      previous: 'b'.repeat(64),
    })
    render(<SkinConsentDialog />)
    fireEvent.click(screen.getByText('仅用静态样式'))

    expect(saveSkinHookPin).not.toHaveBeenCalled()
    expect(applyPluginSkin).not.toHaveBeenCalled()
    expect(useSkinConsentStore.getState().pending).toBeNull()
  })
})
