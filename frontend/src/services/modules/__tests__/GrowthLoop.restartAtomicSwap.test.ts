// @feature: FP-T12 前端连接层/渲染链路 | @ci: frontend-test
/**
 * BUG-26 回归：restartGrowthLoop 重启在飞期注册表不得出现可观测空窗。
 *
 * 根因：restartGrowthLoop 先同步 contributionRegistry.clear() 再 await 异步拉取
 * schema——拉取窗口内 getPages() 为空，而侧栏按钮列表经 1.5s 轮询滞后仍可见，
 * 点击（如 user_admin 声明的 /admin）落入 opener 的「声明缺失」失败分支：
 * 无新 tab、error 通知 8s 自灭（GUI 黑盒观测即「点击无响应、无报错」）。
 *
 * 契约：装载面换装由 loadFromSchema 自身一次同步清空+重注册完成（原子换装），
 * 重启在飞期旧声明集保持可解析；新声明集到位后禁用插件的页面即消失
 * （声明驱动语义不变，失败口径 = opener 既有 error 通知契约）。
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  fetchSchemaCached: vi.fn(),
  invalidateSchemaCache: vi.fn(),
  loadDshAdapterContributions: vi.fn(),
  initResyncOnSchema: vi.fn(),
  disposeResyncOnSchema: vi.fn(),
}))

vi.mock('@/hooks/queries/useSchemaQuery', () => ({
  fetchSchemaCached: mocks.fetchSchemaCached,
  invalidateSchemaCache: mocks.invalidateSchemaCache,
}))
vi.mock('@/services/dshAdapter', () => ({
  loadDshAdapterContributions: mocks.loadDshAdapterContributions,
}))
vi.mock('@/services/websocket/resync', () => ({
  initResyncOnSchema: mocks.initResyncOnSchema,
  disposeResyncOnSchema: mocks.disposeResyncOnSchema,
}))

import { initializeGrowthLoop, restartGrowthLoop } from '@/services/modules/GrowthLoop'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'

/** 生产声明形态：monitoring（无 when）+ user_admin /admin（when: user.role == 'admin'） */
function schemaWithUserAdmin() {
  return {
    plugin_contributes: [
      {
        plugin_id: 'monitoring',
        plugin_name: 'Monitoring',
        contributes: {
          pages: [
            {
              id: 'monitoring',
              title: '监控',
              icon: '📊',
              space: 'workspace',
              slot: 'activity-bar',
              order: 20,
              path: '/monitoring',
              widget: 'widget_stage',
              props: { space: 'monitoring' },
            },
          ],
        },
      },
      {
        plugin_id: 'user_admin',
        plugin_name: 'User Admin HTTP Face',
        contributes: {
          pages: [
            {
              id: 'admin',
              title: '用户管理',
              icon: '👥',
              space: 'workspace',
              slot: 'activity-bar',
              order: 15,
              path: '/admin',
              widget: 'widget_stage',
              when: "user.role == 'admin'",
              props: { space: 'admin' },
            },
          ],
        },
      },
    ],
    plugin_configs: [],
  }
}

/** user_admin 禁用后的 schema（contributes 不再出口） */
function schemaWithoutUserAdmin() {
  const schema = schemaWithUserAdmin()
  schema.plugin_contributes = schema.plugin_contributes.filter(
    (c) => c.plugin_id !== 'user_admin',
  )
  return schema
}

beforeEach(() => {
  useLayoutModeStore.setState({ workspaceTabs: [], visitedTabIds: [] })
  useNotificationStore.getState().clearAll()
  contributionRegistry.clear()
  mocks.fetchSchemaCached.mockReset()
  mocks.loadDshAdapterContributions.mockReset().mockResolvedValue(undefined)
  mocks.invalidateSchemaCache.mockReset().mockResolvedValue(undefined)
})

describe('BUG-26：restart 重启在飞期注册表无空窗', () => {
  it('重启拉取在飞时 /admin 声明仍可解析并落 tab（旧声明集保持可解析）', async () => {
    mocks.fetchSchemaCached.mockResolvedValueOnce(schemaWithUserAdmin())
    await initializeGrowthLoop()

    // 登录重鉴权触发 restartGrowthLoop：拉取挂起（网络在飞）
    let resolveRestart!: (schema: unknown) => void
    mocks.fetchSchemaCached.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRestart = resolve
        }),
    )
    const restartPromise = restartGrowthLoop()

    // 在飞期点击「用户管理」：声明仍可解析，tab 落面板且激活（与「监控」同表现）
    expect(openWorkspacePanelByPath('/admin')).toBe(true)
    const tab = useLayoutModeStore
      .getState()
      .workspaceTabs.find((t) => t.id === 'ws-plugin-admin')
    expect(tab).toBeDefined()
    expect(tab?.isActive).toBe(true)
    expect(tab?.component).toBe('widget_stage')
    expect(tab?.props).toEqual({ space: 'admin' })
    // 声明来自 user_admin 插件（moduleId 命名空间），非 contrib 兜底
    expect(tab?.moduleId).toBe('__plugin_user_admin__')

    // 换装完成：新声明集仍含 /admin，语义不变
    resolveRestart(schemaWithUserAdmin())
    await restartPromise
    expect(openWorkspacePanelByPath('/admin')).toBe(true)
  })

  it('when 条件不参与 opener 解析：声明在即可开（when 仅裁决侧栏可见性）', async () => {
    mocks.fetchSchemaCached.mockResolvedValueOnce(schemaWithUserAdmin())
    await initializeGrowthLoop()

    const declaration = contributionRegistry.getPages().find((p) => p.path === '/admin')
    expect(declaration).toBeDefined()
    expect(declaration?.when).toBe("user.role == 'admin'")

    // opener 无用户上下文也不受 when 影响——权限语义归侧栏可见性过滤
    expect(openWorkspacePanelByPath('/admin')).toBe(true)
    expect(openWorkspacePanelByPath('/monitoring')).toBe(true)
  })

  it('换装后禁用插件的声明即消失：/admin 不再命中并显式报错（不静默）', async () => {
    mocks.fetchSchemaCached.mockResolvedValueOnce(schemaWithUserAdmin())
    await initializeGrowthLoop()

    let resolveRestart!: (schema: unknown) => void
    mocks.fetchSchemaCached.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolveRestart = resolve
        }),
    )
    const restartPromise = restartGrowthLoop()
    resolveRestart(schemaWithoutUserAdmin())
    await restartPromise

    expect(openWorkspacePanelByPath('/admin')).toBe(false)
    const errors = useNotificationStore
      .getState()
      .notifications.filter((n) => n.category === 'error')
    expect(errors.some((n) => n.message.includes('/admin'))).toBe(true)
  })
})
