/**
 * ContributionRegistry — 统一贡献注册中心
 *
 * ADR §六：前端建立 ContributionRegistry 作为唯一真相源，
 * 所有插件贡献（导航、布局、配置、UI 插槽）从这里派生。
 *
 * 数据源：/api/v1/schema 聚合的插件 manifest contributes.*
 *
 * 统一模型（docs/working/重要设计/前端能力统一架构.md 第四章）：
 * 插件贡献收敛为 contributes.pages[]（展示类）。旧 15 种贡献点 key
 * （viewsContainers/views/workspaceTabs/dockItems/floating/modal/statusBarItems/
 * menus/commands/shortcuts/chatMessages/chatInteractions/chatActions/
 * settingsPanels/widgets）在注册时**直接归一化**为 PageDeclaration 存进 pages
 * 集合（pages 是唯一存储，无第二套 entries），旧 key 经 legacyFrom 字段标记来源。
 * 旧查询方法（getViewsContainers/getStatusBarItems/getMenus/...）只是 pages 之上的
 * 薄视图，供渲染侧逐步迁移；渲染侧最终统一消费 getPages()/getPagesBySpace()。
 */

import type { PluginTheme } from '@/types/theme'

/** 贡献点类型（含统一模型的 'pages'；旧类型仍可被薄视图查询） */
export type ContributionType =
  | 'pages'            // 统一页面/元素声明（唯一真相源）
  | 'viewsContainers'  // ActivityBar 一级导航
  | 'views'            // 侧边栏视图
  | 'workspaceTabs'    // 工作区标签页
  | 'dockItems'        // 底部 dock 栏
  | 'floating'         // 浮窗
  | 'modal'            // 模态对话框
  | 'statusBarItems'   // 状态栏条目
  | 'menus'            // 右键/顶栏菜单
  | 'commands'         // 命令面板
  | 'shortcuts'        // 快捷键
  | 'chatMessages'     // 聊天消息卡片样式
  | 'chatInteractions' // 聊天交互模式
  | 'chatActions'      // 聊天输入区动作
  | 'settingsPanels'   // 插件配置面板
  | 'widgets'          // 预置 widget 注册

/** 页面目标空间（contributes.pages[].space） */
export type PageSpace = 'settings' | 'workspace' | 'chat' | 'floating' | 'dock' | 'fullscreen'

/** 页面栏位（contributes.pages[].slot；activity-bar 为旧 viewsContainers 归一化专用） */
export type PageSlot =
  | 'nav'
  | 'tab'
  | 'inline'
  | 'input-action'
  | 'message-style'
  | 'panel'
  | 'item'
  | 'status'
  | 'overlay'
  | 'activity-bar'

/** 页面可脱离宿主容器的三档弹出配置（contributes.pages[].detachable） */
export interface PageDetachable {
  /** 独立浮窗（可拖拽缩放） */
  popout?: boolean
  /** 子窗口（跨页面窗口） */
  childWindow?: boolean
  /** 桌面小组件 */
  desktopWidget?: boolean
  /** 窗口状态持久化 */
  persist?: boolean
  /** 默认尺寸 */
  defaultSize?: { w: number; h: number }
  /** 最小尺寸 */
  minSize?: { w: number; h: number }
  /** 置顶 */
  alwaysOnTop?: boolean
  /** 隐藏于任务栏 */
  skipTaskbar?: boolean
}

/**
 * 统一页面声明（contributes.pages[] + 旧贡献点归一化产物）
 *
 * 字段透传：旧类型专有字段（command/key/location/category/trigger/containerId/
 * openOn 等）经扩展索引原样保留，供旧查询薄视图与 commandDispatcher 等消费。
 */
export interface PageDeclaration {
  /** 统一贡献点类型：固定为 'pages' */
  type: 'pages'
  /** 唯一标识 */
  id: string
  /** 显示名称 */
  title?: string
  /** 图标 */
  icon?: string
  /** 目标空间（6 空间之一） */
  space: PageSpace
  /** 空间内栏位 */
  slot?: PageSlot
  /** 路由路径（可选，用于直达） */
  path?: string
  /** 排序权重 */
  order?: number
  /** 可见条件（when 表达式） */
  when?: string
  /** 激活触发器（旧 modal 用） */
  openOn?: string
  /** 数据源（GET 读 / PUT 写） */
  datasourceUri?: string
  /** L1+ 字段级 schema */
  schema?: Record<string, unknown>
  /** L2+ 布局分组 */
  layout?: Array<Record<string, unknown>>
  /** L3 整体自定义组件（与 schema 互斥） */
  widget?: string
  /** widget 配置/数据源 */
  props?: Record<string, unknown>
  /** 是否可写（配置类 page 用） */
  writable?: boolean
  /** 脱离宿主配置 */
  detachable?: PageDetachable
  /** 配置文件路径（旧 settingsPanels 用） */
  configPath?: string
  /** 配置标签（旧 settingsPanels 用） */
  configLabel?: string
  /** 所属插件 ID */
  pluginId?: string
  /**
   * 归一化来源标记：
   * - 缺省：插件直接声明的 contributes.pages
   * - 'viewsContainers'/'menus'/...：旧贡献点 key 归一化而来
   * - 'settingsPanels'：来自 plugin_configs 的 config_files
   */
  legacyFrom?: string
  /** 扩展字段（旧类型专有字段透传：command/key/location/category/trigger/containerId 等） */
  [key: string]: unknown
}

/** 单个贡献条目（旧贡献点条目形态，兼容消费方；新数据统一以 PageDeclaration 存储） */
export interface ContributionEntry {
  /** 贡献点类型 */
  type: ContributionType
  /** 唯一标识 */
  id: string
  /** 显示名称 */
  title?: string
  /** 分类（命令面板按 category 搜索；来自插件声明，非 ContributionEntry 契约核心字段） */
  category?: string
  /** 图标 */
  icon?: string
  /** 何时可见（条件表达式） */
  when?: string
  /** 何时激活（触发器） */
  openOn?: string
  /** 使用哪个预置 widget */
  widget?: string
  /** widget 配置/数据源 */
  props?: Record<string, unknown>
  /** 排序权重 */
  order?: number
  /** 所属插件 ID */
  pluginId?: string
  /** 配置文件路径（settingsPanels 用） */
  configPath?: string
  /** 配置标签（settingsPanels 用） */
  configLabel?: string
  /** 路由路径（viewsContainers 用） */
  path?: string
  /** 扩展字段 */
  [key: string]: unknown
}

/** 配置面板条目（settingsPanels 专用，来自 plugin_configs 独立数据源） */
export interface SettingsPanelEntry {
  pluginId: string
  pluginName: string
  pluginIcon?: string
  configFiles: Array<{
    id: string
    path: string
    label: string
  }>
}

/** 插件 ui_schema 声明的单个 widget */
export interface WidgetDeclaration {
  /** widget 实例标识（插件内唯一；与槽位 id 相等时视为对该槽位的覆盖声明） */
  id: string
  /** widget 类型，对应 WidgetRegistry 注册 key */
  type: string
  /** 目标渲染空间 */
  space?: string
  /** 触发时机 */
  trigger?: string
  /** 排序权重（槽位内多声明裁决：小者胜；缺省 1000） */
  order?: number
  /** widget props */
  props?: Record<string, unknown>
  /** 来源插件 ID */
  pluginId?: string
}

/** 插件 CSS 注入声明（contributes.client_styles 条目） */
export interface ClientStyleDeclaration {
  /** 样式标识（插件内唯一，全局唯一键为 `{pluginId}:{id}`） */
  id: string
  /** CSS 资源路径（相对插件根，如 "/assets/border.css"；拼接为 /ext/{pluginId}{path}） */
  path: string
  /** 作用域：global 不包装（装饰全局）；scoped 自动加 [data-plugin] 前缀（防全局污染） */
  scope?: 'global' | 'scoped'
  /** 描述 */
  description?: string
  /** 来源插件 ID */
  pluginId: string
}

/** contributes 中旁路注册（不归一化为页面）的视觉贡献 key */
const NON_PAGE_CONTRIBUTE_KEYS: ReadonlySet<string> = new Set(['themes', 'client_styles'])

/**
 * 已弃用的旧贡献 key（ADR 2026-08-17 widget-migration-t8-t13-t14）：
 * 声明这些 key 的插件数据被忽略（不进 pages 归一化）。
 * - workspaceTabs：薄视图零调用方，工作区标签统一走 contributes.pages
 * - chatMessages/chatInteractions/chatActions：chat/inline 槽无渲染方，
 *   消息级内联卡片场景已被工具卡协议（ui.chat_card / render）覆盖
 */
const DEPRECATED_CONTRIBUTE_KEYS: ReadonlySet<string> = new Set([
  'workspaceTabs',
  'chatMessages',
  'chatInteractions',
  'chatActions',
])

/**
 * 旧贡献点 key → 归一化 space/slot 映射（统一架构 4.4 节）
 *
 * 交互类（menus/commands/shortcuts/modal）在 actions 模型落地前暂归一化为
 * 页面占位（legacyFrom 标记真实来源，旧查询薄视图按 legacyFrom 检索，不受影响）。
 */
const LEGACY_PAGE_MAP: Record<string, { space: PageSpace; slot: PageSlot }> = {
  viewsContainers: { space: 'workspace', slot: 'activity-bar' },
  views: { space: 'workspace', slot: 'tab' },
  dockItems: { space: 'dock', slot: 'item' },
  statusBarItems: { space: 'dock', slot: 'status' },
  floating: { space: 'floating', slot: 'panel' },
  modal: { space: 'floating', slot: 'overlay' },
  menus: { space: 'chat', slot: 'inline' },
  commands: { space: 'chat', slot: 'input-action' },
  shortcuts: { space: 'chat', slot: 'input-action' },
  settingsPanels: { space: 'settings', slot: 'nav' },
  widgets: { space: 'workspace', slot: 'tab' },
  // [弃用 2026-08-17，ADR widget-migration-t8-t13-t14] workspaceTabs /
  // chatMessages / chatInteractions / chatActions 四个旧贡献 key 归一化已移除：
  // - workspaceTabs 薄视图（getWorkspaceTabs）零调用方——工作区标签统一走
  //   contributes.pages + openWorkspacePanelByPath（插件声明 path 直达）
  // - chat/inline 槽自始无渲染方——消息级内联卡片场景已被工具卡协议
  //   （ui.chat_card / render）覆盖，声明这些 key 的插件数据不再进 pages
}

/** PageDeclaration 显式字段（归一化时其余字段原样透传） */
const PAGE_EXPLICIT_KEYS = new Set([
  'id', 'title', 'icon', 'space', 'slot', 'path', 'order', 'when', 'openOn',
  'datasourceUri', 'schema', 'layout', 'widget', 'props', 'writable', 'detachable',
  'configPath', 'configLabel', 'type', 'pluginId', 'legacyFrom',
])

/**
 * ContributionRegistry
 *
 * 全局单例，管理所有插件贡献。
 * 数据源来自 /api/v1/schema 聚合。
 *
 * 存储模型：pages（PageDeclaration[]）是**唯一**贡献存储；
 * settingsPanels（plugin_configs 配置注册表）与 widgetsByPlugin（ui_schema
 * 声明）是独立数据源的旁路注册表，保持原 API。
 */
export class ContributionRegistry {
  /** 归一化页面集合（唯一真相源，含声明页 + 旧贡献点归一化页） */
  private pages: PageDeclaration[] = []
  /** 页面集合的 pluginId 索引（同一对象引用，非第二套存储） */
  private pagesByPlugin: Map<string, PageDeclaration[]> = new Map()
  /** 配置面板条目（plugin_configs 独立数据源） */
  private settingsPanels: Map<string, SettingsPanelEntry> = new Map()
  /** 插件 widget 声明（按 pluginId 索引，来自 ui_schema） */
  private widgetsByPlugin: Map<string, WidgetDeclaration[]> = new Map()
  /** 插件主题声明（contributes.themes 旁路注册，不归一化为页面） */
  private pluginThemes: PluginTheme[] = []
  /** 插件 CSS 注入声明（contributes.client_styles 旁路注册，不归一化为页面） */
  private clientStyles: ClientStyleDeclaration[] = []
  /** 是否已初始化 */
  private initialized = false

  /**
   * 从 /api/v1/schema 响应加载贡献（含 plugin_configs）
   *
   * 重新加载会清空旧数据（幂等重载，避免幽灵菜单/widget）。
   *
   * @param schema - SchemaResponse 对象
   */
  loadFromSchema(schema: Record<string, unknown>): void {
    // 幂等重载：先清空旧状态
    this.clear()

    // 加载 plugin_configs（配置面板注册表 + 归一化为 settings/nav 页面）
    const pluginConfigs = schema.plugin_configs as
      | Array<{ plugin_id: string; plugin_name: string; config_files: Array<{ id: string; path: string; label: string; target?: string; fields?: unknown[] }> }>
      | undefined

    if (Array.isArray(pluginConfigs)) {
      for (const entry of pluginConfigs) {
        this.settingsPanels.set(entry.plugin_id, {
          pluginId: entry.plugin_id,
          pluginName: entry.plugin_name,
          configFiles: entry.config_files,
        })
        // config_files → settings 页（datasourceUri 指向配置文件路径）
        for (const file of entry.config_files) {
          this.registerPage({
            type: 'pages',
            id: `${entry.plugin_id}:${file.id}`,
            title: file.label,
            space: 'settings',
            slot: 'nav',
            datasourceUri: file.path,
            pluginId: entry.plugin_id,
            legacyFrom: 'settingsPanels',
          })
        }
      }
    }

    // 加载 plugin_contributes（contributes 贡献点，统一归一化为 pages）
    this.registerFromSchema(
      schema as unknown as Parameters<typeof this.registerFromSchema>[0],
    )

    // 提取 agents / pipelines 的 ui_schema.widgets
    this.extractWidgets(schema)
  }

  /**
   * 从 agents / pipelines 的 ui_schema 提取 widget 声明
   */
  private extractWidgets(schema: Record<string, unknown>): void {
    const sources: Array<{ list?: Array<Record<string, unknown>> }> = [
      { list: schema.agents as Array<Record<string, unknown>> | undefined },
      { list: schema.pipelines as Array<Record<string, unknown>> | undefined },
    ]
    for (const { list } of sources) {
      if (!Array.isArray(list)) continue
      for (const entry of list) {
        const pluginId = entry.id as string | undefined
        if (!pluginId) continue
        const uiSchema = entry.ui_schema as { widgets?: Array<Record<string, unknown>> } | null | undefined
        if (!uiSchema || !Array.isArray(uiSchema.widgets)) continue
        const widgets: WidgetDeclaration[] = uiSchema.widgets.map((w) => ({
          id: w.id as string,
          type: w.type as string,
          space: w.space as string | undefined,
          trigger: w.trigger as string | undefined,
          order: typeof w.order === 'number' ? w.order : undefined,
          props: w.props as Record<string, unknown> | undefined,
          pluginId,
        }))
        this.widgetsByPlugin.set(pluginId, widgets)
      }
    }
  }

  /**
   * 获取指定插件的配置文件列表
   */
  getPluginConfigFiles(pluginId: string): Array<{ id: string; path: string; label: string; target?: string; fields?: unknown[] }> {
    return this.settingsPanels.get(pluginId)?.configFiles ?? []
  }

  /**
   * 获取全部插件配置条目
   */
  getPluginConfigs(): SettingsPanelEntry[] {
    return Array.from(this.settingsPanels.values())
  }

  /**
   * 从 schema 数据注册贡献点。
   *
   * 数据源是后端 `/api/v1/schema` 的 `plugin_contributes` 字段：每项形如
   * `{ plugin_id, plugin_name, contributes }`，其中 `contributes` 是 manifest 原样透传的
   * `Record<ContributionType, item[]>`（内核不解释结构）。
   *
   * 所有 key（含旧 15 种贡献点）在注册时**直接归一化**为 PageDeclaration 存入
   * pages 集合；旧 key 经 legacyFrom 标记来源。settingsPanels 另在
   * `loadFromSchema` 经 `plugin_configs` 字段归一化（plugin_configs 注册表）。
   *
   * @param schemaData - /api/v1/schema 返回的聚合数据
   */
  registerFromSchema(schemaData: {
    plugin_contributes?: Array<{
      plugin_id: string
      plugin_name?: string
      contributes?: Record<string, unknown[]>
    }>
  }): void {
    if (!schemaData.plugin_contributes) return

    for (const entry of schemaData.plugin_contributes) {
      const pluginId = entry.plugin_id
      const contributes = entry.contributes
      if (!contributes) continue

      // 遍历每种贡献点 key（pages / viewsContainers / views / menus / ...），统一归一化
      // 例外：themes / client_styles 是纯数据视觉贡献（主题变量 + CSS），不进 pages 归一化，
      // 由各自旁路注册表承接（themeStore / pluginStyles 消费）。
      for (const [type, items] of Object.entries(contributes)) {
        if (!Array.isArray(items)) continue
        if (DEPRECATED_CONTRIBUTE_KEYS.has(type)) continue
        if (NON_PAGE_CONTRIBUTE_KEYS.has(type)) {
          if (type === 'themes') {
            this.registerPluginThemes(pluginId, items as Record<string, unknown>[])
          } else {
            this.registerClientStyles(pluginId, items as Record<string, unknown>[])
          }
          continue
        }
        for (const item of items) {
          this.normalizeAndRegister(type, item as Record<string, unknown>, pluginId)
        }
      }
    }

    this.initialized = true
  }

  /**
   * 注册单个贡献条目（归一化为 page 存入 pages 集合）
   */
  register(entry: ContributionEntry): void {
    this.normalizeAndRegister(entry.type, { ...entry } as unknown as Record<string, unknown>, entry.pluginId ?? '')
  }

  /**
   * 注销贡献条目（按 id 从 pages 集合移除）
   */
  unregister(type: ContributionType, id: string): void {
    const before = this.pages.length
    this.pages = this.pages.filter((p) => !(p.id === id && (type === 'pages' ? !p.legacyFrom : p.legacyFrom === type)))
    if (this.pages.length === before) return
    // 重建 pluginId 索引
    this.pagesByPlugin.clear()
    for (const page of this.pages) {
      const pid = page.pluginId ?? 'unknown'
      const list = this.pagesByPlugin.get(pid) ?? []
      list.push(page)
      this.pagesByPlugin.set(pid, list)
    }
  }

  /**
   * 获取某类型的所有贡献（薄视图）
   *
   * - 'pages' → 插件直接声明的页面（无 legacyFrom）
   * - 旧类型 key → 归一化页面中 legacyFrom === type 的项
   */
  getByType(type: ContributionType): PageDeclaration[] {
    if (type === 'pages') return this.pages.filter((p) => !p.legacyFrom)
    return this.pages.filter((p) => p.legacyFrom === type)
  }

  // ── 统一模型查询（pages 集合）──

  /**
   * 获取全部页面（声明页 + 旧贡献点归一化页，按 order 排序）
   */
  getPages(): PageDeclaration[] {
    return [...this.pages]
  }

  /**
   * 按空间获取页面（settings/workspace/chat/floating/dock/fullscreen）
   */
  getPagesBySpace(space: PageSpace): PageDeclaration[] {
    return this.pages.filter((p) => p.space === space)
  }

  /**
   * 按 id 查找页面
   */
  getPage(id: string): PageDeclaration | undefined {
    return this.pages.find((p) => p.id === id)
  }

  /**
   * 获取指定插件的全部页面（声明页 + 归一化页）
   */
  getPluginPages(pluginId: string): PageDeclaration[] {
    return [...(this.pagesByPlugin.get(pluginId) ?? [])]
  }

  // ── 旧贡献点查询（仅保留仍有生产消费的薄视图；getViewsContainers/
  // getStatusBarItems/getDockItems 零消费，已清理——统一走 getPagesBySpace）──

  /**
   * 获取侧边栏视图（归一化前为 views；Sidebar 容器视图消费）
   */
  getViews(containerId?: string): PageDeclaration[] {
    const views = this.pages.filter((p) => p.legacyFrom === 'views')
    if (containerId) {
      return views.filter((v) => v.containerId === containerId)
    }
    return views
  }

  /**
   * 获取配置面板列表（plugin_configs 注册表）
   */
  getSettingsPanels(): SettingsPanelEntry[] {
    return Array.from(this.settingsPanels.values())
  }

  /**
   * 获取指定插件的配置面板
   */
  getSettingsPanel(pluginId: string): SettingsPanelEntry | undefined {
    return this.settingsPanels.get(pluginId)
  }

  // ── Widget 声明（来自 agents/pipelines 的 ui_schema）──

  /**
   * 获取指定插件的 widget 声明（来自 ui_schema.widgets）
   */
  getWidgetsForPlugin(pluginId: string): WidgetDeclaration[] {
    return this.widgetsByPlugin.get(pluginId) ?? []
  }

  /**
   * 聚合所有插件的 widget 声明
   */
  getAllWidgets(): WidgetDeclaration[] {
    const all: WidgetDeclaration[] = []
    for (const list of this.widgetsByPlugin.values()) {
      all.push(...list)
    }
    return all
  }

  // ── 插件主题（contributes.themes 旁路注册）──

  /**
   * 获取全部插件贡献的主题
   */
  getPluginThemes(): PluginTheme[] {
    return [...this.pluginThemes]
  }

  /**
   * 按 id 查找插件主题（id 即 plugin.json 声明的 id；插件间同名由后注册者覆盖）
   */
  getPluginTheme(themeId: string): PluginTheme | undefined {
    return this.pluginThemes.find((t) => t.id === themeId)
  }

  /**
   * 获取指定插件的主题
   */
  getThemesForPlugin(pluginId: string): PluginTheme[] {
    return this.pluginThemes.filter((t) => t.pluginId === pluginId)
  }

  // ── 插件 CSS 注入（contributes.client_styles 旁路注册）──

  /**
   * 获取全部插件 CSS 注入声明
   */
  getClientStyles(): ClientStyleDeclaration[] {
    return [...this.clientStyles]
  }

  /**
   * 获取指定插件的 CSS 注入声明
   */
  getClientStylesForPlugin(pluginId: string): ClientStyleDeclaration[] {
    return this.clientStyles.filter((s) => s.pluginId === pluginId)
  }

  // ── 旧交互类贡献点查询（pages 之上的薄视图）──

  /**
   * 获取菜单项（归一化前为 menus，按 location 过滤）
   *
   * @param location - 菜单位置（如 'workspace/context'、'chat/context'）；省略返回全部
   */
  getMenus(location?: string): PageDeclaration[] {
    const menus = this.pages.filter((p) => p.legacyFrom === 'menus')
    if (location === undefined) return menus
    return menus.filter((m) => m.location === location)
  }

  /**
   * 获取所有命令（归一化前为 commands，命令面板聚合用）
   */
  getCommands(): PageDeclaration[] {
    return this.pages.filter((p) => p.legacyFrom === 'commands')
  }

  /**
   * 获取所有快捷键绑定（归一化前为 shortcuts）
   */
  getShortcuts(): PageDeclaration[] {
    return this.pages.filter((p) => p.legacyFrom === 'shortcuts')
  }

  /**
   * 获取所有模态弹窗声明（归一化前为 modal）
   */
  getModals(): PageDeclaration[] {
    return this.pages.filter((p) => p.legacyFrom === 'modal')
  }

  /**
   * 按 trigger 查找模态弹窗（如 'on_command:xxx'）
   */
  findModalByTrigger(trigger: string): PageDeclaration | undefined {
    return this.getModals().find((m) => m.openOn === trigger || m.trigger === trigger)
  }

  /**
   * 检查是否已初始化
   */
  isInitialized(): boolean {
    return this.initialized
  }

  /**
   * 该插件是否声明了配置（config_files）
   */
  hasPluginConfig(pluginId: string): boolean {
    return this.settingsPanels.has(pluginId)
  }

  /**
   * 清空所有注册
   */
  clear(): void {
    this.pages = []
    this.pagesByPlugin.clear()
    this.settingsPanels.clear()
    this.widgetsByPlugin.clear()
    this.pluginThemes = []
    this.clientStyles = []
    this.initialized = false
  }

  // ── 内部：旁路注册（themes / client_styles，不归一化为页面）──

  /**
   * 注册插件主题（contributes.themes）
   *
   * 幂等：同 pluginId + id 重复声明只更新不追加。
   * 同名 id 冲突（跨插件）：后注册者覆盖（风险 §四.1，缓解靠插件前缀约定）。
   */
  private registerPluginThemes(pluginId: string, items: Array<Record<string, unknown>>): void {
    for (const raw of items) {
      if (typeof raw.id !== 'string' || typeof raw.name !== 'string') continue
      const base = raw.base === 'light' ? 'light' : 'dark'
      const theme: PluginTheme = {
        id: raw.id,
        name: raw.name,
        description: typeof raw.description === 'string' ? raw.description : undefined,
        base,
        variables:
          raw.variables && typeof raw.variables === 'object'
            ? (raw.variables as Record<string, string>)
            : undefined,
        backgrounds:
          raw.backgrounds && typeof raw.backgrounds === 'object'
            ? (raw.backgrounds as PluginTheme['backgrounds'])
            : undefined,
        pluginId,
      }
      const idx = this.pluginThemes.findIndex((t) => t.pluginId === pluginId && t.id === theme.id)
      if (idx >= 0) this.pluginThemes[idx] = theme
      else this.pluginThemes.push(theme)
    }
  }

  /**
   * 注册插件 CSS 注入声明（contributes.client_styles）
   *
   * 幂等：同 pluginId + id 重复声明只更新不追加。
   */
  private registerClientStyles(pluginId: string, items: Array<Record<string, unknown>>): void {
    for (const raw of items) {
      if (typeof raw.id !== 'string' || typeof raw.path !== 'string') continue
      const style: ClientStyleDeclaration = {
        id: raw.id,
        path: raw.path,
        scope: raw.scope === 'scoped' ? 'scoped' : 'global',
        description: typeof raw.description === 'string' ? raw.description : undefined,
        pluginId,
      }
      const idx = this.clientStyles.findIndex((s) => s.pluginId === pluginId && s.id === style.id)
      if (idx >= 0) this.clientStyles[idx] = style
      else this.clientStyles.push(style)
    }
  }

  // ── 内部：归一化注册 ──

  /**
   * 归一化注册单个贡献项为 PageDeclaration
   *
   * - type === 'pages'：按声明原样注册
   * - 旧贡献点 key：按 LEGACY_PAGE_MAP 映射 space/slot，legacyFrom 标记来源
   * - 未知 key：兜底归一化（不丢弃），space/slot 取默认值
   */
  private normalizeAndRegister(type: string, item: Record<string, unknown>, pluginId: string): void {
    if (type === 'pages') {
      this.registerPage(toPageDeclaration(item, pluginId))
      return
    }
    const mapping = LEGACY_PAGE_MAP[type]
    if (mapping) {
      this.registerPage(toPageDeclaration(item, pluginId, { space: mapping.space, slot: mapping.slot, legacyFrom: type }))
    } else {
      this.registerPage(toPageDeclaration(item, pluginId, { space: 'workspace', slot: 'tab', legacyFrom: type }))
    }
  }

  /**
   * 注册页面（按 id 去重，pages 与 pagesByPlugin 同步，按 order 排序）
   */
  private registerPage(page: PageDeclaration): void {
    if (this.pages.some((p) => p.id === page.id)) return
    this.pages.push(page)
    this.pages.sort((a, b) => (a.order ?? 50) - (b.order ?? 50))

    const pid = page.pluginId ?? 'unknown'
    const list = this.pagesByPlugin.get(pid) ?? []
    list.push(page)
    list.sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
    this.pagesByPlugin.set(pid, list)
  }
}

/** 全局单例 */
export const contributionRegistry = new ContributionRegistry()

/**
 * 将贡献项转换为 PageDeclaration
 *
 * 显式字段做类型收窄；其余字段（command/key/location/category/trigger/
 * containerId 等旧类型专有字段）原样透传到扩展索引，保证旧查询薄视图可用。
 *
 * @param item - 贡献项原始数据（manifest 原样透传）
 * @param pluginId - 所属插件
 * @param override - 归一化覆盖（space/slot 映射与 legacyFrom 来源标记）
 */
function toPageDeclaration(
  item: Record<string, unknown>,
  pluginId: string,
  override?: { space?: PageSpace; slot?: PageSlot; legacyFrom?: string },
): PageDeclaration {
  const id = (item.id as string | undefined) ?? synthesizeId(override?.legacyFrom ?? 'pages', pluginId, item)
  const page: PageDeclaration = {
    type: 'pages',
    id,
    title: item.title as string | undefined,
    icon: item.icon as string | undefined,
    space: (override?.space ?? (item.space as PageSpace | undefined)) ?? 'workspace',
    slot: override?.slot ?? (item.slot as PageSlot | undefined),
    path: item.path as string | undefined,
    order: item.order as number | undefined,
    when: item.when as string | undefined,
    openOn: item.openOn as string | undefined,
    datasourceUri: item.datasourceUri as string | undefined,
    schema: item.schema as Record<string, unknown> | undefined,
    layout: item.layout as Array<Record<string, unknown>> | undefined,
    widget: item.widget as string | undefined,
    props: item.props as Record<string, unknown> | undefined,
    writable: item.writable as boolean | undefined,
    detachable: item.detachable as PageDetachable | undefined,
    configPath: item.configPath as string | undefined,
    configLabel: item.configLabel as string | undefined,
    pluginId,
    legacyFrom: override?.legacyFrom,
  }
  // 透传旧类型/扩展字段（command/key/location/category/trigger/containerId 等）
  for (const [key, value] of Object.entries(item)) {
    if (PAGE_EXPLICIT_KEYS.has(key)) continue
    page[key] = value
  }
  return page
}

/**
 * 为无显式 id 的贡献条目合成稳定 id
 *
 * shortcuts/menu 条目常无 id 字段，需从其标识字段合成以避免误去重：
 * - shortcuts：command（每命令一个绑定）
 * - menus：command + location（同命令在不同位置可各一）
 * - 其他：pluginId + type + 序列
 */
function synthesizeId(type: string, pluginId: string, item: Record<string, unknown>): string {
  if (type === 'shortcuts' && typeof item.command === 'string') {
    return `${pluginId}:shortcut:${item.command}`
  }
  if (type === 'menus') {
    const cmd = typeof item.command === 'string' ? item.command : ''
    const loc = typeof item.location === 'string' ? item.location : ''
    return `${pluginId}:menu:${cmd}:${loc}`
  }
  if (typeof item.command === 'string') return `${pluginId}:${type}:${item.command}`
  if (typeof item.title === 'string') return `${pluginId}:${type}:${item.title}`
  return `${pluginId}:${type}:${JSON.stringify(item).slice(0, 32)}`
}
