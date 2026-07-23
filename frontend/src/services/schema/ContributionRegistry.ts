/**
 * ContributionRegistry —— 插件能力聚合数据层（非视觉）
 *
 * 聚合 /api/v1/schema 返回的插件能力（config_files + ui_schema，以及后续 contributes），
 * 提供按插件/全局的查询接口。
 *
 * 边界（严格）：
 * - 只做"数据聚合 + 查询"，不做任何渲染、不引用 React 组件、不与 WidgetRegistry 耦合；
 * - WidgetRegistry（WidgetRegistry.ts）负责"组件类型 → React 组件"映射，本注册表只
 *   负责"插件声明了哪些 widget"，二者解耦。
 *
 * @module schema/ContributionRegistry
 */

import type { SchemaResponse, PluginUiWidget } from '@/services/api/schema'

/** 单个配置文件映射项（manifest config_files 的前端镜像） */
export interface ContributionConfigFile {
  id: string
  path: string
  label: string
}

/** 插件配置条目（schema.plugin_configs 元素） */
export interface ContributionPluginConfig {
  plugin_id: string
  plugin_name: string
  config_files: ContributionConfigFile[]
}

/** 带 pluginId 来源标记的 widget 声明（供渲染层查到组件类型后定位来源插件） */
export interface ContributionWidget extends PluginUiWidget {
  /** 声明该 widget 的插件 id */
  pluginId: string
}

/**
 * 插件能力聚合注册表。
 *
 * 一次 loadFromSchema 后即可按 pluginId / 全局查询 config_files 与 ui_schema widgets。
 * 再次调用 loadFromSchema 覆盖旧数据（幂等重载）。
 */
export class ContributionRegistry {
  /** pluginId → 配置条目 */
  private readonly pluginConfigs: Map<string, ContributionPluginConfig> = new Map()
  /** pluginId → widget 声明列表 */
  private readonly pluginWidgets: Map<string, ContributionWidget[]> = new Map()

  /**
   * 从 schema 聚合响应载入插件能力（覆盖旧数据）。
   *
   * @param schema - /api/v1/schema 聚合响应
   */
  loadFromSchema(schema: SchemaResponse): void {
    this.pluginConfigs.clear()
    this.pluginWidgets.clear()

    // config_files 聚合
    const entries = (schema as SchemaResponse & {
      plugin_configs?: ContributionPluginConfig[]
    }).plugin_configs
    if (Array.isArray(entries)) {
      for (const entry of entries) {
        this.pluginConfigs.set(entry.plugin_id, entry)
      }
    }

    // ui_schema 聚合：agents + pipelines 都可能声明 ui_schema
    for (const agent of schema.agents ?? []) {
      this.collectWidgets(agent.id, agent.ui_schema)
    }
    for (const pipeline of schema.pipelines ?? []) {
      this.collectWidgets(pipeline.id, pipeline.ui_schema)
    }
  }

  /**
   * 把单个条目的 ui_schema widgets 收集进注册表。
   */
  private collectWidgets(
    pluginId: string,
    uiSchema: { widgets?: PluginUiWidget[] } | null | undefined,
  ): void {
    if (!uiSchema || !Array.isArray(uiSchema.widgets) || uiSchema.widgets.length === 0) return
    const stamped: ContributionWidget[] = uiSchema.widgets.map((w) => ({ ...w, pluginId }))
    this.pluginWidgets.set(pluginId, stamped)
  }

  /**
   * 返回全部插件配置条目。
   */
  getPluginConfigs(): ContributionPluginConfig[] {
    return Array.from(this.pluginConfigs.values())
  }

  /**
   * 某插件是否声明了配置文件。
   */
  hasPluginConfig(pluginId: string): boolean {
    return this.pluginConfigs.has(pluginId)
  }

  /**
   * 按 pluginId 取配置文件列表（无声明返回空数组）。
   */
  getPluginConfigFiles(pluginId: string): ContributionConfigFile[] {
    return this.pluginConfigs.get(pluginId)?.config_files ?? []
  }

  /**
   * 按 pluginId 取其声明的 widget 列表（无声明返回空数组）。
   */
  getWidgetsForPlugin(pluginId: string): ContributionWidget[] {
    return this.pluginWidgets.get(pluginId) ?? []
  }

  /**
   * 取所有插件声明的全部 widget（携带来源 pluginId）。
   */
  getAllWidgets(): ContributionWidget[] {
    return Array.from(this.pluginWidgets.values()).flat()
  }
}

/** ContributionRegistry 全局单例 */
export const contributionRegistry = new ContributionRegistry()

export default contributionRegistry
