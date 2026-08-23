/**
 * 皮肤运行时激活链端到端（2026-08-22 平台化：声明驱动，DSH 皮肤只是消费者之一）
 *
 * 链路：contributionRegistry.registerFromSchema（内核 schema 真实形态）
 * → themeStore.setTheme → loadTheme pluginTheme 分支
 * → applyTheme + applyPluginThemeVars + skinRuntime（平台皮肤运行时）：
 *   - html[data-skin="<pluginId>:<skin>"] 平台 scope 打标（暗色补 body[data-skin-dark]）
 *   - 皮肤合并 css 经 /ext/{pluginId}/styles/skin/{skin}/merged.css 按择注入
 *     <style>（data-theme-style="skin-<pluginId>:<skin>"）
 *   - 切换/移除 → 属性与 <style> 摘除
 *
 * 平台化断言：非 DSH 插件（自写皮肤插件）声明 skin 字段即同链路生效——
 * 不依赖 dsh_adapter，与 DSH 皮肤同一运行时。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { contributionRegistry } from '@/services/schema/ContributionRegistry'

// 皮肤合并 css / hooks 走插件通道拉取——mock 网络层，返回值由用例控制
// （hooks 文本同 mock 值：vitest Node 环境 blob import 必失败被捕获，
// 动态层跳过不影响本文件的静态层断言）
vi.mock('@/services/api/client', () => ({
  apiClient: { get: vi.fn() },
}))
import { apiClient } from '@/services/api/client'
const fetchMock = apiClient.get as unknown as ReturnType<typeof vi.fn>

const MIKU_THEME = {
  id: 'dsh-skin-miku',
  name: '初音未来 · 电子歌姬',
  description: 'DSH 皮肤',
  base: 'light',
  skin: 'miku',
  variables: {
    '--ds-bg-canvas': '#eef5ff',
    '--ds-accent-primary': '#2e9bff',
    '--background': '214 100% 97%',
    '--bubble-ai-mode': 'flat',
  },
  backgrounds: {
    image: {
      enabled: true,
      url: '/ext/dsh_adapter/styles/skin-assets/miku/assets/miku-art-light.jpg',
      position: 'center',
      size: 'cover',
      attachment: 'fixed',
      overlay: 'rgba(244, 250, 255, 0.5)',
      overlayOpacity: 1,
    },
  },
}

/** 非 DSH 插件的皮肤声明：自写皮肤插件（免适配器）走同一运行时 */
const OWN_THEME = {
  id: 'my-skin-a',
  name: '自有皮肤 A',
  base: 'dark',
  skin: 'a',
  variables: { '--ds-accent-primary': '#ff6f00' },
}

function cleanupDom(): void {
  localStorage.clear()
  document.body.className = ''
  document.documentElement.removeAttribute('style')
  document.documentElement.removeAttribute('data-skin')
  document.body.removeAttribute('data-skin-dark')
  document.querySelectorAll('style[data-theme-style^="skin-"]').forEach((el) => el.remove())
}

describe('themeStore 皮肤激活链（平台运行时）', () => {
  beforeEach(() => {
    cleanupDom()
    contributionRegistry.clear()
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'dsh_adapter', plugin_name: 'DSH Adapter', contributes: { themes: [MIKU_THEME] } },
        { plugin_id: 'my_skins', plugin_name: '自有皮肤插件', contributes: { themes: [OWN_THEME] } },
      ],
    })
    fetchMock.mockReset()
    fetchMock.mockResolvedValue({ data: '/* skin css */ html[data-skin="x"] * { border-radius: 6px !important; }' } as never)
  })

  afterEach(cleanupDom)

  it('选择皮肤主题：插件主题命中并激活立绘背景层', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')

    const state = useThemeStore.getState()
    expect(state.activePluginTheme?.id).toBe('dsh-skin-miku')

    const htmlStyle = document.documentElement.getAttribute('style') ?? ''
    expect(htmlStyle).toContain('--ds-bg-canvas')
    expect(htmlStyle).toContain('--bg-image')
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
  })

  it('AI 消息气泡形态随主题声明：皮肤主题 --bubble-ai-mode=flat → 平铺；内置主题回退 bubble', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')
    expect(useThemeStore.getState().bubbleAiMode).toBe('flat')

    await useThemeStore.getState().setTheme('dark')
    expect(useThemeStore.getState().bubbleAiMode).toBe('bubble')
  })

  it('按择注入：激活皮肤打平台 scope 标（data-skin=插件:皮肤）并注入合并 css', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')
    await vi.waitFor(() => {
      expect(document.documentElement.getAttribute('data-skin')).toBe('dsh_adapter:miku')
    })
    expect(document.body.hasAttribute('data-skin-dark')).toBe(false) // light 底不打暗色开关
    const styleEl = document.querySelector('style[data-theme-style="skin-dsh_adapter:miku"]')
    expect(styleEl).toBeTruthy()
    expect(styleEl?.textContent).toContain('border-radius: 6px')
    expect(fetchMock).toHaveBeenCalledWith(
      '/ext/dsh_adapter/styles/skin/miku/merged.css',
      expect.anything(),
    )
  })

  it('自写皮肤插件（非 DSH、免适配器）：同一声明链路生效', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('my-skin-a')
    await vi.waitFor(() => {
      expect(document.documentElement.getAttribute('data-skin')).toBe('my_skins:a')
    })
    expect(document.body.hasAttribute('data-skin-dark')).toBe(true) // dark 底打暗色开关
    const styleEl = document.querySelector('style[data-theme-style="skin-my_skins:a"]')
    expect(styleEl).toBeTruthy()
    expect(fetchMock).toHaveBeenCalledWith(
      '/ext/my_skins/styles/skin/a/merged.css',
      expect.anything(),
    )
    expect(document.documentElement.style.getPropertyValue('--ds-accent-primary')).toBe('#ff6f00')
  })

  it('切回内置主题：平台标记与注入的 <style> 一并摘除', async () => {
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')
    await vi.waitFor(() => expect(document.querySelector('style[data-theme-style="skin-dsh_adapter:miku"]')).toBeTruthy())

    await useThemeStore.getState().setTheme('dark')
    expect(document.documentElement.getAttribute('data-skin')).toBeNull()
    expect(document.querySelector('style[data-theme-style="skin-dsh_adapter:miku"]')).toBeNull()
    expect(useThemeStore.getState().activePluginTheme).toBeNull()
  })

  it('退役方案残留的 dsh-skin-* 用户主题被清理且不截胡', async () => {
    // 模拟已删除的 dshSkinTheme.ts 写入的残留
    localStorage.setItem(
      'theme_user_custom',
      JSON.stringify([
        { id: 'dsh-skin-miku', name: '旧残留', basedOn: 'dark', customizations: {}, updatedAt: '2026-08-20T00:00:00Z' },
      ]),
    )
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku')

    // 残留被删 + 插件主题生效（立绘层激活证明未走残留分支）
    const stored = localStorage.getItem('theme_user_custom') ?? '[]'
    expect(JSON.parse(stored).find((t: { id: string }) => t.id === 'dsh-skin-miku')).toBeUndefined()
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
  })
})

describe('会话恢复时序（registry 未就绪挂起→注册后重放）', () => {
  it('registry 未就绪时插件主题挂起不回退，retry 后恢复', async () => {
    cleanupDom()
    // 模拟会话启动时序：registry 未注册（reset 清空 + initialized=false）
    contributionRegistry.clear()
    const { useThemeStore } = await import('@/stores/themeStore')
    await useThemeStore.getState().setTheme('dsh-skin-miku') // persist 恢复路径等价

    // registry 就绪前重新 loadTheme（刷新时序）：挂起、不回退、不覆盖选择
    await useThemeStore.getState().loadTheme('dsh-skin-miku')
    const state = useThemeStore.getState()
    expect(state.pendingThemeId).toBe('dsh-skin-miku')
    expect(state.currentThemeId).not.toBe('dark') // 未被回退覆盖

    // schema 注册（growthLoop）→ 重放
    contributionRegistry.registerFromSchema({
      plugin_contributes: [
        { plugin_id: 'dsh_adapter', contributes: { themes: [MIKU_THEME] } },
      ],
    })
    await useThemeStore.getState().retryPendingTheme()
    const after = useThemeStore.getState()
    expect(after.pendingThemeId).toBeNull()
    expect(after.activePluginTheme?.id).toBe('dsh-skin-miku')
    expect(document.body.classList.contains('has-bg-image')).toBe(true)
    expect(document.documentElement.style.getPropertyValue('--ds-bg-canvas')).toBe('#eef5ff')
  })
})
