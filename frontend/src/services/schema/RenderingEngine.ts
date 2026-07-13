/**
 * 渲染引擎
 *
 * 接收 ParsedSchema，生成渲染指令（RenderInstruction）。
 * 支持按渲染空间分类、客户端能力降级、渲染指令缓存。
 *
 * @module RenderingEngine
 */

import { widgetRegistry } from './WidgetRegistry'
import type {
  ParsedSchema,
  RenderingSpaceType,
  RenderingSpaceConfig,
  ClientCapabilities,
  ChatInteractionConfig,
} from '@/types/schema'

/** 渲染指令 */
export interface RenderInstruction {
  /** 指令唯一 ID */
  id: string
  /** 目标渲染空间 */
  space: RenderingSpaceType
  /** Widget 组件类型 */
  widgetType: string
  /** Widget 组件（已从 WidgetRegistry 解析） */
  component: React.ComponentType<Record<string, unknown>> | null
  /** 传递给组件的属性 */
  props: Record<string, unknown>
  /** 数据源引用 */
  dataSource?: string
  /** 布局配置 */
  layout?: RenderingSpaceConfig['layout']
  /** 来源模块 ID */
  moduleId: string
}

/** 渲染指令集 */
export interface RenderInstructionSet {
  /** 按渲染空间分类的指令 */
  bySpace: Record<RenderingSpaceType, RenderInstruction[]>
  /** 所有指令的平铺列表 */
  all: RenderInstruction[]
  /** 来源 Schema 的版本哈希 */
  versionHash: string
  /** 生成时间戳 */
  generatedAt: number
}

/** 渲染引擎配置 */
export interface RenderingEngineConfig {
  /** 是否启用缓存（默认 true） */
  enableCache?: boolean
  /** 是否启用客户端能力降级（默认 true） */
  enableDegradation?: boolean
}

/**
 * 渲染引擎
 *
 * 将 ParsedSchema 转换为 RenderInstruction 集合。
 * 核心流程：解析渲染配置 → 匹配 Widget → 过滤降级 → 缓存输出。
 *
 * @example
 * ```ts
 * const engine = new RenderingEngine()
 * const instructions = engine.render(parsedSchema)
 *
 * // 获取聊天空间指令
 * const chatInstructions = instructions.bySpace.chat
 *
 * // 获取所有指令
 * const allInstructions = instructions.all
 * ```
 */
export class RenderingEngine {
  /** 指令缓存：versionHash → RenderInstructionSet */
  private readonly cache: Map<string, RenderInstructionSet> = new Map()
  /** 引擎配置 */
  private readonly config: Required<RenderingEngineConfig>

  /**
   * @param config - 渲染引擎配置
   */
  constructor(config?: RenderingEngineConfig) {
    this.config = {
      enableCache: config?.enableCache ?? true,
      enableDegradation: config?.enableDegradation ?? true,
    }
  }

  /**
   * 根据 ParsedSchema 生成渲染指令
   *
   * @param schema - 解析后的 Schema
   * @param capabilities - 可选的客户端能力（用于降级过滤）
   * @returns 渲染指令集
   */
  render(
    schema: ParsedSchema,
    capabilities?: ClientCapabilities,
  ): RenderInstructionSet {
    // 1. 检查缓存
    const cacheKey = schema.versionHash
    if (this.config.enableCache) {
      const cached = this.cache.get(cacheKey)
      if (cached) return cached
    }

    // 2. 生成指令
    const spaceInstructions = this.generateInstructions(schema, capabilities)
    const all = this.flattenInstructions(spaceInstructions)

    const result: RenderInstructionSet = {
      bySpace: spaceInstructions,
      all,
      versionHash: schema.versionHash,
      generatedAt: Date.now(),
    }

    // 3. 写入缓存
    if (this.config.enableCache) {
      this.cache.set(cacheKey, result)
    }

    return result
  }

  /**
   * 批量渲染多个 ParsedSchema
   *
   * @param schemas - 解析后的 Schema 数组
   * @param capabilities - 可选的客户端能力
   * @returns 合并后的渲染指令集
   */
  renderAll(
    schemas: ParsedSchema[],
    capabilities?: ClientCapabilities,
  ): RenderInstructionSet {
    const allBySpace = this.createEmptySpaceMap()
    const allInstructions: RenderInstruction[] = []

    for (const schema of schemas) {
      const { bySpace, all } = this.render(schema, capabilities)
      for (const [space, instructions] of Object.entries(bySpace)) {
        allBySpace[space as RenderingSpaceType].push(...instructions)
      }
      allInstructions.push(...all)
    }

    return {
      bySpace: allBySpace,
      all: allInstructions,
      versionHash: schemas.map((s) => s.versionHash).join('+'),
      generatedAt: Date.now(),
    }
  }

  /**
   * 清空渲染指令缓存
   */
  clearCache(): void {
    this.cache.clear()
  }

  /**
   * 生成各空间的渲染指令
   */
  private generateInstructions(
    schema: ParsedSchema,
    capabilities?: ClientCapabilities,
  ): Record<RenderingSpaceType, RenderInstruction[]> {
    const result = this.createEmptySpaceMap()
    const moduleId = schema.identity.id

    // 支持的渲染空间集合
    const supportedSpaces = this.resolveSupportedSpaces(schema, capabilities)

    // 1. 处理 rendering.spaces
    for (const spaceConfig of schema.rendering.spaces) {
      if (!supportedSpaces.has(spaceConfig.space)) continue

      const instruction = this.createInstructionFromSpace(
        spaceConfig,
        moduleId,
        schema.versionHash,
      )
      if (instruction) {
        result[spaceConfig.space].push(instruction)
      }
    }

    // 2. 处理 rendering.chat → chat 空间
    if (supportedSpaces.has('chat')) {
      for (const chatConfig of schema.rendering.chat) {
        const instruction = this.createInstructionFromChat(
          chatConfig,
          moduleId,
          schema.versionHash,
        )
        if (instruction) {
          result.chat.push(instruction)
        }
      }
    }

    // 3. 处理 rendering.dock → dock 空间
    if (supportedSpaces.has('dock') && schema.rendering.dock) {
      const instruction = this.createInstructionForDock(
        schema,
        schema.versionHash,
      )
      if (instruction) {
        result.dock.push(instruction)
      }
    }

    return result
  }

  /**
   * 从 RenderingSpaceConfig 创建渲染指令
   */
  private createInstructionFromSpace(
    config: RenderingSpaceConfig,
    moduleId: string,
    versionHash: string,
  ): RenderInstruction | null {
    // 查找组件（支持降级）
    const component = this.config.enableDegradation
      ? widgetRegistry.findFallback(config.widget)
      : widgetRegistry.get(config.widget) ?? null

    return {
      id: `${moduleId}::${config.space}::${config.widget}::${versionHash}`,
      space: config.space,
      widgetType: config.widget,
      component,
      props: config.props ?? {},
      dataSource: config.dataSource,
      layout: config.layout,
      moduleId,
    }
  }

  /**
   * 从 ChatInteractionConfig 创建渲染指令
   */
  private createInstructionFromChat(
    config: ChatInteractionConfig,
    moduleId: string,
    versionHash: string,
  ): RenderInstruction | null {
    const widgetType = config.type
    const component = this.config.enableDegradation
      ? widgetRegistry.findFallback(widgetType)
      : widgetRegistry.get(widgetType) ?? null

    return {
      id: `${moduleId}::chat::${widgetType}::${versionHash}`,
      space: 'chat',
      widgetType,
      component,
      props: config.props ?? {},
      dataSource: config.dataSource,
      moduleId,
    }
  }

  /**
   * 为 Dock 空间创建渲染指令
   */
  private createInstructionForDock(
    schema: ParsedSchema,
    versionHash: string,
  ): RenderInstruction | null {
    const moduleId = schema.identity.id
    const dockConfig = schema.rendering.dock

    return {
      id: `${moduleId}::dock::entry::${versionHash}`,
      space: 'dock',
      widgetType: 'dock_entry',
      component: null, // Dock 入口由 DockSpaceRenderer 自行处理
      props: {
        icon: dockConfig?.icon ?? schema.identity.icon,
        label: dockConfig?.label ?? schema.identity.name,
        indicator: dockConfig?.indicator ?? 'none',
        indicatorColor: dockConfig?.indicatorColor,
        moduleId,
      },
      moduleId,
    }
  }

  /**
   * 解析客户端支持的渲染空间
   */
  private resolveSupportedSpaces(
    schema: ParsedSchema,
    capabilities?: ClientCapabilities,
  ): Set<RenderingSpaceType> {
    const allSpaces: RenderingSpaceType[] = [
      'chat',
      'workspace',
      'floating',
      'dock',
      'fullscreen',
    ]

    // 如果有降级 fallback 配置，优先使用 fallback 空间
    if (capabilities?.fallback) {
      const fallbackSpaces = new Set<RenderingSpaceType>([
        ...capabilities.requiredSpaces,
        capabilities.fallback.space,
      ])
      return fallbackSpaces
    }

    if (capabilities?.requiredSpaces) {
      return new Set(capabilities.requiredSpaces)
    }

    // 无能力限制时，Schema 自身声明的空间 + chat（默认）
    const declaredSpaces = new Set<RenderingSpaceType>(['chat'])
    schema.rendering.spaces.forEach((s) => declaredSpaces.add(s.space))
    if (schema.rendering.dock) declaredSpaces.add('dock')

    return declaredSpaces
  }

  /**
   * 创建空的渲染空间映射
   */
  private createEmptySpaceMap(): Record<RenderingSpaceType, RenderInstruction[]> {
    return {
      chat: [],
      workspace: [],
      floating: [],
      dock: [],
      fullscreen: [],
    }
  }

  /**
   * 将按空间分类的指令平铺为数组
   */
  private flattenInstructions(
    bySpace: Record<RenderingSpaceType, RenderInstruction[]>,
  ): RenderInstruction[] {
    return Object.values(bySpace).flat()
  }
}

/** 渲染引擎单例 */
export const renderingEngine = new RenderingEngine()

export default renderingEngine
