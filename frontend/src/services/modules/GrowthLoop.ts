/** 自生长闭环集成 连接模块管理器、Schema 注册表、WebSocket 推送和组件注册 */

import apiClient from '@/services/api/client'
import { fetchSchemaCached, invalidateSchemaCache } from '@/hooks/queries/useSchemaQuery'
import { syncPluginStyles, removeAllPluginStyles } from '@/services/pluginStyles'
import { commandDispatcher } from '@/services/schema/commandDispatcher'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { initializeWidgets } from '@/services/schema/registerWidgets'
import { shortcutRegistry } from '@/services/schema/shortcutRegistry'
import { useLayoutModeStore } from '@/stores/layoutModeStore'
import { useNotificationStore } from '@/stores/notificationStore'
import { useThemeStore } from '@/stores/themeStore'
import { loggers } from '@/utils/logger'
import { loadChatCardDeclarations } from '@/utils/chatCardInterpreter'
import { disposeResyncOnSchema, initResyncOnSchema } from '@/services/websocket/resync'
import { loadDshAdapterContributions } from '@/services/dshAdapter'
import { loadRenderIntents } from '@/utils/dshRenderIntent'
import { loadOutputSchemas } from '@/utils/outputSchemaView'
import { loadInteractionModes } from '@/utils/interactionModes'
import { loadNotificationModes } from '@/utils/notificationModes'
import { loadViewModes } from '@/utils/viewModeRoutes'
import { validatePluginDeclaration } from '@/services/pluginDeclarationValidate'
import type { ChatCardDeclaration } from '@/utils/chatCardInterpreter'

/** 初始化自生长闭环 1. 注册所有预置组件 */
export async function initializeGrowthLoop(): Promise<void> {
  loggers.websocket.info('正在初始化自生长闭环...')

  // Step 0: 注入命令内核 transport（命令面板/快捷键/菜单 → 内核 capability 出口）
  commandDispatcher.setTransport(async (commandId, args) => {
    await apiClient.post('/api/v1/actions/execute', { action: commandId, args })
  })

  // Step 1: 注册预置组件
  initializeWidgets()
  loggers.websocket.info('预置组件注册完成')

  // Step 2: 加载 schema 到 ContributionRegistry 并同步导航（0.2 核心数据源）
  await reloadContributionRegistry()

  // Step 3: 订阅 resync_required（内核重启/回放缓冲 miss 时的被动全量重同步通道）
  initResyncOnSchema()

  loggers.websocket.info('自生长闭环初始化完成')
}

/**
 * 重新拉取 schema 并刷新 ContributionRegistry + 导航 + 快捷键 + 插件主题/CSS。
 *
 * 集中了 contributes 数据的加载逻辑，供初始化、重启、schema_updated 事件复用。
 * 失败不阻塞主流程（刻意设计），但须可见降级：console.warn 留痕 + 非阻塞通知
 * （节流去重，避免 schema_updated 重复事件刷屏）——否则用户视角"插件功能
 * 整体消失"无任何指示。
 */
let _lastSchemaLoadNotifyAt = 0

async function reloadContributionRegistry(): Promise<void> {
  try {
    // 缓存新鲜直读（与设置页共享同一 query 条目），事件路径已先行 invalidate
    const schema = await fetchSchemaCached()
    contributionRegistry.loadFromSchema(schema as unknown as Record<string, unknown>)
    // 插件主题（如 DSH 皮肤）的会话恢复重放：主题初始化早于本注册，
    // 挂起的 pendingThemeId 现在能查到了（时序修复配套，2026-08-21）
    void useThemeStore.getState().retryPendingTheme()
    // 工具卡片声明（chat_card）从 tools[].ui.chat_card 装载到解释器注册表
    loadChatCardDeclarations(
      (schema as { tools?: Array<{ name?: string; ui?: { chat_card?: ChatCardDeclaration } }> }).tools ?? [],
    )
    // 工具输出契约（output_schema，widget 化 T4）：从 tools[].output_schema 装载，
    // 无声明的工具按契约渲染只读结构化视图 + 违规标警
    loadOutputSchemas(
      (schema as { tools?: Array<{ name?: string; output_schema?: Record<string, unknown> }> }).tools ?? [],
    )
    // 交互模式布局声明（widget 化 T9）：human_interaction_tool 的
    // tools[].ui.interaction_modes 装载（模式→features 词表，覆盖内置默认件）
    loadInteractionModes(
      (schema as { tools?: Array<{ ui?: { interaction_modes?: unknown } }> }).tools ?? [],
    )
    // 通知分类渲染声明（widget 化批1-C）：human_interaction_tool 的
    // tools[].ui.notification_modes 装载（category→渲染词表，覆盖内置默认件）
    loadNotificationModes(
      (schema as { tools?: Array<{ ui?: { notification_modes?: unknown } }> }).tools ?? [],
    )
    // 审批视图模式声明（widget 化 T10）：review_service 的 tools[].ui.view_modes
    // 装载（view_mode→widget 路由，ApprovalRouter 声明驱动查找）
    loadViewModes(
      (schema as { tools?: Array<{ ui?: { view_modes?: unknown } }> }).tools ?? [],
    )
    // render 意图声明：tools[].render 装载到 dshRenderIntent 注册表（声明路由），
    // 无声明时工具结果按数据形状自动路由（数据路由），均未命中落通用数据渲染
    loadRenderIntents(
      (schema as { tools?: Array<{ name?: string; render?: Record<string, unknown> }> }).tools ?? [],
    )
    // 插件声明合法性校验（Phase 1-C5）：装载时对页面/工具渲染/chat_card/widget 声明
    // 做结构校验，坏声明不再静默降级——errors/warnings 统一收集上报（健康度观察）。
    try {
      const schemaVal = schema as unknown as Record<string, unknown>
      const pluginContribPages = (((schemaVal.plugin_contributes as
        Array<Record<string, unknown>> | undefined) ?? [])
        .flatMap((c) => ((c.contributes as Record<string, unknown[]> | undefined)?.pages ?? [])))
      const widgetSources = [
        ...(((schemaVal.agents as Array<Record<string, unknown>> | undefined) ?? [])),
        ...(((schemaVal.pipelines as Array<Record<string, unknown>> | undefined) ?? [])),
      ]
        .map((a) => a.ui_schema)
        .filter((u): u is { widgets?: unknown } => !!u && typeof u === 'object')
      const declResult = validatePluginDeclaration({
        pages: pluginContribPages as Array<Record<string, unknown>>,
        tools: (schemaVal.tools as Array<Record<string, unknown>> | undefined) ?? [],
        uiSchemaWidgets: widgetSources.flatMap((u) => (u.widgets ?? []) as never[]),
        streaming: (schemaVal.capabilities as { streaming?: Record<string, unknown> } | undefined)?.streaming,
      })
      if (declResult.errors.length > 0) {
        loggers.websocket.warn(
          `插件声明校验不通过（${declResult.errors.length} 处，声明可能不生效）:\n${declResult.errors.slice(0, 20).join('\n')}`,
        )
      }
      if (declResult.warnings.length > 0) {
        loggers.websocket.warn(
          `插件声明降级警告（${declResult.warnings.length} 处）:\n${declResult.warnings.slice(0, 20).join('\n')}`,
        )
      }
    } catch (err) {
      loggers.websocket.warn('插件声明校验异常:', err)
    }
    shortcutRegistry.refresh()

    // DSH 适配器贡献（task_dsh_plugin_adapter 任务 2）：renderers 兜底注册 +
    // 来源版本记录。失败隔离在服务内部（不影响本函数其余步骤）。
    await loadDshAdapterContributions()

    // 插件视觉贡献同步（contributes.themes / client_styles）：
    // - 主题：插件主题合入主题列表；当前用的插件主题被移除（插件禁用）→ 回退 base
    // - CSS：以注册表为权威注入新样式 / 移除失效样式（禁用插件无残留）
    useThemeStore.getState().syncPluginThemes()
    syncPluginStyles(contributionRegistry.getClientStyles())
  } catch (error) {
    loggers.websocket.warn('ContributionRegistry 加载失败:', error)
    // 可见降级（FE11）：插件贡献（pages/导航/命令/卡片）单一装载点失败时
    // 发非阻塞通知；60s 节流防 schema_updated 连发刷屏
    const now = Date.now()
    if (now - _lastSchemaLoadNotifyAt > 60_000) {
      _lastSchemaLoadNotifyAt = now
      useNotificationStore.getState().addNotification({
        category: 'error',
        title: '插件贡献加载失败',
        message: `页面/导航/命令等插件功能暂不可用（${error instanceof Error ? error.message : String(error)}），将在下次 schema 更新时自动重试。`,
        priority: 'normal',
        isBlocking: false,
        autoDismissMs: 10_000,
      })
    }
  }
}

/**
 * 主动刷新插件贡献（插件启用/禁用切换后调用，无需 WS 事件）
 *
 * 插件禁用语义：contributes 不再导出 → schema 重载后其主题从列表移除、
 * 注入 CSS 被清理；当前在用其主题时回退 base（syncPluginThemes 内处理）。
 */
export function refreshPluginContributions(): Promise<void> {
  // 插件启停即刻生效：先失效缓存，避免 staleTime 窗口内拿到旧 contributes
  return invalidateSchemaCache().then(() => reloadContributionRegistry())
}

/** 销毁自生长闭环（完全清理） 用于登出、认证过期等场景，需要彻底清除所有模块状态。 */
export function destroyGrowthLoop(): void {
  // 先注销 resync 订阅并取消未触发的防抖（globalWS.disconnect 也会清 handler，双保险）
  disposeResyncOnSchema()
  contributionRegistry.clear()
  // 插件注入样式随闭环销毁清除（防跨会话残留）
  removeAllPluginStyles()
  const store = useLayoutModeStore.getState()
  store.setDockItems([])
  useLayoutModeStore.setState({ workspaceTabs: [] })
}

/** 重新启动自生长闭环 原子性替换，避免清空后再拉取导致工作区闪烁 */
export async function restartGrowthLoop(): Promise<void> {
  contributionRegistry.clear()

  initializeWidgets()

  // 补挂 resync_required 订阅（登出 disconnect 会清空全部 handler，幂等可重复调用）
  initResyncOnSchema()

  try {
    // 重新加载 schema 到 ContributionRegistry（0.2 核心数据源）
    await reloadContributionRegistry()
    loggers.websocket.info('自生长闭环重启完成')
  } catch (error) {
    useLayoutModeStore.setState({ workspaceTabs: [] })
    useLayoutModeStore.getState().setDockItems([])
    throw error
  }
}
