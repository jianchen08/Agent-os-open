/** @feature FP-0.2.通知音效 目录/偏好读取（getNotificationSoundOptions/Id 直测） | @ci: frontend-test */
/**
 * 音效目录与偏好读取的独立直测（独立文件＝干净 catalogPromise 模块态，
 * 不受既有播放用例的成功缓存影响）：目录成功/失败折算、偏好读取存储不可用
 * 回落空串。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const catalog = vi.hoisted(() => ({ fetchNotificationSoundCatalog: vi.fn() }))

vi.mock('@/services/api/notificationSounds', () => ({
  fetchNotificationSoundCatalog: (...args: unknown[]) => catalog.fetchNotificationSoundCatalog(...args),
}))

import { getNotificationSoundId, getNotificationSoundOptions } from '@/utils/audioNotification'

beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
})

describe('getNotificationSoundOptions 目录拉取', () => {
  it('失败：折算空目录（展示「不可用」），不抛错', async () => {
    catalog.fetchNotificationSoundCatalog.mockRejectedValue(new Error('503'))
    await expect(getNotificationSoundOptions()).resolves.toEqual({ sounds: [], defaultId: '' })
  })

  it('成功：透传目录与默认项', async () => {
    const sounds = [{ id: 'chime', label: '清铃' }]
    catalog.fetchNotificationSoundCatalog.mockResolvedValue({ sounds, defaultId: 'chime' })
    await expect(getNotificationSoundOptions()).resolves.toEqual({ sounds, defaultId: 'chime' })
  })
})

describe('getNotificationSoundId 偏好读取', () => {
  it('localStorage 抛错：回落空串（未选择语义）', () => {
    const spy = vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('gone')
    })
    expect(getNotificationSoundId()).toBe('')
    spy.mockRestore()
  })
})
