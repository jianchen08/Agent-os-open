/**
 * ContributionRegistry — 统一贡献注册中心
 *
 * ADR §六：前端建立 ContributionRegistry 作为唯一真相源，
 * 所有插件贡献（导航、布局、配置、UI 插槽）从这里派生。
 *
 * 四个投影：
 * - NavigationProvider → 顶栏/侧栏导航项
 * - LayoutProvider → 布局区的存在与拓扑
 * - SettingsProvider → 设置菜单分组 + 配置页
 * - WidgetProvider → 预置 Widget 注册
 *
 * 数据源：/api/v1/schema 聚合的插件 manifest contributes.*
 */

import React from 'react'

/** 贡献点类型 */
export type ContributionType =
  | 'viewsContainers'   // ActivityBar 一级导航
  | 'views'             // 侧边栏视图
  | 'workspaceTabs'     // 工作区标签页
  | 'dockItems'         // 底部 dock 栏
  | 'floating'          // 浮窗
  | 'modal'             // 模态对话框
  | 'statusBarItems'    // 状态栏条目
  | 'menus'             // 右键/顶栏菜单
  | 'commands'          // 命令面板
  | 'shortcuts'         // 快捷键
  | 'chatMessages'      // 聊天消息卡片样式
  | 'chatInteractions'  // 聊天交互模式
  | 'chatActions'       // 聊天输入区动作
  | 'settingsPanels'    // 插件配置面板
  | 'widgets'           // 预置 widget 注册

/** 单个贡献条目 */
export interface ContributionEntry {
  /** 贡献点类型 */
  type: ContributionType
  /** 唯一标识 */
  id: string
  /** 显示名称 */
  title?: string
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

/** 配置面板条目（settingsPanels 专用） */
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
  /** widget 实例标识（插件内唯一） */
  id: string
  /** widget 类型，对应 WidgetRegistry 注册 key */
  type: string
  /** 目标渲染空间 */
  space?: string
  /** 触发时机 */
  trigger?: string
  /** widget props */
  props?: Record<string, unknown>
  /** 来源插件 ID */
  pluginId?: string
}

/**
 * ContributionRegistry
 *
 * 全局单例，管理所有插件贡献。
 * 数据源来自 /api/v1/schema 聚合。
 */
export class ContributionRegistry {
  /** 所有贡献条目 */
  private entries: Map<string, ContributionEntry[]> = new Map()
  /** 配置面板条目 */
  private settingsPanels: Map<string, SettingsPanelEntry> = new Map()
  /** 插件 widget 声明（按 pluginId 索引，来自 ui_schema） */
  private widgetsByPlugin: Map<string, WidgetDeclaration[]> = new Map()
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

    // 加载 plugin_configs（配置面板）
    const pluginConfigs = schema.plugin_configs as
      | Array<{ plugin_id: string; plugin_name: string; config_files: Array<{ id: string; path: string; label: string }> }>
      | undefined

    if (Array.isArray(pluginConfigs)) {
      for (const entry of pluginConfigs) {
        this.settingsPanels.set(entry.plugin_id, {
          pluginId: entry.plugin_id,
          pluginName: entry.plugin_name,
          configFiles: entry.config_files,
        })
      }
    }

    // 加载 plugin_contributes（contributes 贡献点）
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
  getPluginConfigFiles(pluginId: string): Array<{ id: string; path: string; label: string }> {
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
   * settingsPanels 不在此处理——已在 `loadFromSchema` 经 `plugin_configs` 字段独立加载。
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

      // 遍历每种贡献点类型（viewsContainers / views / menus / statusBarItems / ...）
      for (const [type, items] of Object.entries(contributes)) {
        if (!Array.isArray(items)) continue
        for (const item of items) {
          this.register({
            ...item,
            id: (item.id as string | undefined) ?? synthesizeId(type, pluginId, item),
            type: type as ContributionType,
            pluginId,
          } as ContributionEntry)
        }
      }
    }

    this.initialized = true
  }

  /**
   * 注册单个贡献条目
   */
  register(entry: ContributionEntry): void {
    const type = entry.type
    if (!this.entries.has(type)) {
      this.entries.set(type, [])
    }
    const list = this.entries.get(type)!
    // 去重
    if (!list.some((e) => e.id === entry.id)) {
      list.push(entry)
      // 按 order 排序
      list.sort((a, b) => (a.order ?? 50) - (b.order ?? 50))
    }
  }

  /**
   * 注销贡献条目
   */
  unregister(type: ContributionType, id: string): void {
    const list = this.entries.get(type)
    if (list) {
      const idx = list.findIndex((e) => e.id === id)
      if (idx >= 0) list.splice(idx, 1)
    }
  }

  /**
   * 获取某类型的所有贡献
   */
  getByType(type: ContributionType): ContributionEntry[] {
    return this.entries.get(type) ?? []
  }

  /**
   * 获取导航项（viewsContainers）
   */
  getViewsContainers(): ContributionEntry[] {
    return this.getByType('viewsContainers')
  }

  /**
   * 获取侧边栏视图（views）
   */
  getViews(containerId?: string): ContributionEntry[] {
    const views = this.getByType('views')
    if (containerId) {
      return views.filter((v) => v.containerId === containerId)
    }
    return views
  }

  /**
   * 获取工作区标签页（workspaceTabs）
   */
  getWorkspaceTabs(): ContributionEntry[] {
    return this.getByType('workspaceTabs')
  }

  /**
   * 获取配置面板列表（settingsPanels）
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

  /**
   * 获取状态栏条目
   */
  getStatusBarItems(): ContributionEntry[] {
    return this.getByType('statusBarItems')
  }

  /**
   * 获取 dock 栏条目
   */
  getDockItems(): ContributionEntry[] {
    return this.getByType('dockItems')
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

  // ── contributes.menus（右键/上下文菜单，ADR §3.4 档位二）──

  /**
   * 获取菜单项（按 location 过滤）
   *
   * @param location - 菜单位置（如 'workspace/context'、'chat/context'）；省略返回全部
   */
  getMenus(location?: string): ContributionEntry[] {
    const menus = this.getByType('menus')
    if (location === undefined) return menus
    return menus.filter((m) => m.location === location)
  }

  // ── contributes.commands（命令面板，ADR §3.4 档位二）──

  /**
   * 获取所有命令（命令面板聚合用）
   */
  getCommands(): ContributionEntry[] {
    return this.getByType('commands')
  }

  // ── contributes.shortcuts（快捷键，ADR §3.4 档位二）──

  /**
   * 获取所有快捷键绑定
   */
  getShortcuts(): ContributionEntry[] {
    return this.getByType('shortcuts')
  }

  // ── contributes.modal（模态弹窗，ADR §3.4 档位二）──

  /**
   * 获取所有模态弹窗声明
   */
  getModals(): ContributionEntry[] {
    return this.getByType('modal')
  }

  /**
   * 按 trigger 查找模态弹窗（如 'on_command:xxx'）
   */
  findModalByTrigger(trigger: string): ContributionEntry | undefined {
    return this.getModals().find((m) => m.openOn === trigger || (m as { trigger?: string }).trigger === trigger)
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
    this.entries.clear()
    this.settingsPanels.clear()
    this.widgetsByPlugin.clear()
    this.initialized = false
  }
}

/** 全局单例 */
export const contributionRegistry = new ContributionRegistry()

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
