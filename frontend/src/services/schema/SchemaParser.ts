/**
 * Schema 解析服务
 *
 * 解析后端 API 返回的 UI Schema JSON，转换为 ParsedSchema。
 * 支持：版本哈希计算、结构验证、增量更新。
 *
 * @module SchemaParser
 */

import type {
  ModuleUISchema,
  ParsedSchema,
  ClientCapabilities,
  RenderingSpaceType,
} from '@/types/schema'

/** Schema 解析错误 */
export class SchemaParseError extends Error {
  constructor(
    message: string,
    public readonly path?: string,
  ) {
    super(message)
    this.name = 'SchemaParseError'
  }
}

/** Schema 结构验证结果 */
export interface ValidationResult {
  /** 是否通过验证 */
  valid: boolean
  /** 验证错误列表 */
  errors: string[]
  /** 验证警告列表 */
  warnings: string[]
}

/** Schema 解析选项 */
export interface SchemaParserOptions {
  /** 是否执行严格验证（默认 true） */
  strict?: boolean
  /** 是否跳过客户端能力过滤（默认 false） */
  skipClientFilter?: boolean
  /** 当前客户端能力 */
  capabilities?: ClientCapabilities
}

/**
 * Schema 解析器
 *
 * 解析后端返回的 ModuleUISchema，输出 ParsedSchema。
 * 支持增量更新：通过版本哈希比较检测变化，仅更新变化的模块。
 *
 * @example
 * ```ts
 * const parser = new SchemaParser()
 * const result = parser.parse(apiSchema)
 * if (result.changed) {
 *   // 更新渲染
 * }
 * ```
 */
export class SchemaParser {
  /** 已解析的 Schema 缓存：moduleId → ParsedSchema */
  private readonly cache: Map<string, ParsedSchema> = new Map()

  /**
   * 解析完整的 ModuleUISchema
   *
   * @param schema - 后端返回的原始 Schema
   * @param options - 解析选项
   * @returns 解析结果，包含 changed 标志用于增量更新
   * @throws SchemaParseError 当 Schema 结构无效时
   */
  parse(
    schema: ModuleUISchema,
    options?: SchemaParserOptions,
  ): { parsed: ParsedSchema; changed: boolean } {
    // 1. 结构验证
    const validation = this.validate(schema)
    if (!validation.valid) {
      throw new SchemaParseError(
        `Schema 验证失败: ${validation.errors.join('; ')}`,
      )
    }

    // 2. 计算版本哈希
    const versionHash = computeVersionHash(schema)
    const moduleId = schema.identity.id

    // 3. 检查增量更新（版本哈希相同则未变化）
    const cached = this.cache.get(moduleId)
    if (cached && cached.versionHash === versionHash) {
      return { parsed: cached, changed: false }
    }

    // 4. 构建 ParsedSchema
    const parsed: ParsedSchema = {
      raw: schema,
      identity: { ...schema.identity },
      actions: [...schema.actions],
      rendering: {
        chat: [...schema.rendering.chat],
        spaces: [...schema.rendering.spaces],
        dock: schema.rendering.dock ? { ...schema.rendering.dock } : undefined,
        fullscreen: schema.rendering.fullscreen
          ? { ...schema.rendering.fullscreen }
          : undefined,
      },
      clients: { ...schema.clients },
      parsedAt: Date.now(),
      versionHash,
    }

    // 5. 缓存
    this.cache.set(moduleId, parsed)

    return { parsed, changed: true }
  }

  /**
   * 批量解析 Schema 列表
   *
   * @param schemas - Schema 数组
   * @param options - 解析选项
   * @returns 解析结果数组
   */
  parseAll(
    schemas: ModuleUISchema[],
    options?: SchemaParserOptions,
  ): Array<{ parsed: ParsedSchema; changed: boolean }> {
    return schemas.map((s) => this.parse(s, options))
  }

  /**
   * 增量更新：对比新旧 Schema，只返回有变化的解析结果
   *
   * @param schemas - 最新的 Schema 列表
   * @param options - 解析选项
   * @returns 有变化的解析结果数组
   */
  parseUpdates(
    schemas: ModuleUISchema[],
    options?: SchemaParserOptions,
  ): Array<{ parsed: ParsedSchema; changed: boolean }> {
    return this.parseAll(schemas, options).filter((r) => r.changed)
  }

  /**
   * 验证 Schema 结构完整性
   *
   * @param schema - 待验证的 Schema
   * @returns 验证结果
   */
  validate(schema: unknown): ValidationResult {
    const errors: string[] = []
    const warnings: string[] = []

    if (!schema || typeof schema !== 'object') {
      errors.push('Schema 必须是非空对象')
      return { valid: false, errors, warnings }
    }

    const s = schema as Record<string, unknown>

    // identity 验证
    if (!s.identity || typeof s.identity !== 'object') {
      errors.push('缺少 identity 字段')
    } else {
      const identity = s.identity as Record<string, unknown>
      if (!identity.id) errors.push('identity.id 不能为空')
      if (!identity.name) errors.push('identity.name 不能为空')
      if (!identity.version) errors.push('identity.version 不能为空')
      if (
        identity.category &&
        !['builtin', 'extension', 'custom'].includes(identity.category as string)
      ) {
        warnings.push(
          `identity.category "${String(identity.category)}" 不是标准分类`,
        )
      }
    }

    // actions 验证
    if (!Array.isArray(s.actions)) {
      errors.push('actions 必须是数组')
    } else {
      s.actions.forEach((action, i) => {
        const a = action as Record<string, unknown>
        if (!a.id) errors.push(`actions[${i}].id 不能为空`)
        if (!a.name) errors.push(`actions[${i}].name 不能为空`)
        if (
          a.type &&
          !['command', 'query', 'event', 'stream'].includes(a.type as string)
        ) {
          warnings.push(
            `actions[${i}].type "${String(a.type)}" 不是标准操作类型`,
          )
        }
      })
    }

    // rendering 验证
    if (!s.rendering || typeof s.rendering !== 'object') {
      errors.push('缺少 rendering 字段')
    } else {
      const rendering = s.rendering as Record<string, unknown>
      if (!Array.isArray(rendering.chat)) {
        errors.push('rendering.chat 必须是数组')
      }
      if (!Array.isArray(rendering.spaces)) {
        errors.push('rendering.spaces 必须是数组')
      }
    }

    // clients 验证
    if (!s.clients || typeof s.clients !== 'object') {
      errors.push('缺少 clients 字段')
    } else {
      const clients = s.clients as Record<string, unknown>
      if (!Array.isArray(clients.requiredSpaces)) {
        errors.push('clients.requiredSpaces 必须是数组')
      }
    }

    return { valid: errors.length === 0, errors, warnings }
  }

  /**
   * 按客户端能力过滤 Schema
   *
   * 过滤掉当前客户端不支持的渲染空间和 Widget。
   *
   * @param schema - 原始 Schema
   * @param capabilities - 客户端能力
   * @returns 过滤后的 Schema（浅拷贝）
   */
  filterByCapabilities(
    schema: ModuleUISchema,
    capabilities: ClientCapabilities,
  ): ModuleUISchema {
    const supportedSpaces = new Set<RenderingSpaceType>(
      capabilities.requiredSpaces,
    )

    const filteredSpaces = schema.rendering.spaces.filter((space) =>
      supportedSpaces.has(space.space),
    )

    const filteredChat = schema.rendering.chat.filter((chat) =>
      capabilities.requiredWidgets.length === 0
        ? true
        : capabilities.requiredWidgets.includes(chat.type),
    )

    return {
      ...schema,
      rendering: {
        ...schema.rendering,
        spaces: filteredSpaces,
        chat: filteredChat,
      },
    }
  }

  /**
   * 获取缓存的 ParsedSchema
   *
   * @param moduleId - 模块 ID
   * @returns 缓存的解析结果或 undefined
   */
  getCached(moduleId: string): ParsedSchema | undefined {
    return this.cache.get(moduleId)
  }

  /**
   * 清空缓存
   */
  clearCache(): void {
    this.cache.clear()
  }
}

/**
 * 计算 Schema 版本哈希
 *
 * 基于 Schema 的 JSON 序列化内容计算哈希值，用于变更检测。
 *
 * @param schema - 模块 Schema
 * @returns 哈希字符串
 */
function computeVersionHash(schema: ModuleUISchema): string {
  const raw = JSON.stringify(schema)
  let hash = 0
  for (let i = 0; i < raw.length; i++) {
    const char = raw.charCodeAt(i)
    hash = (hash << 5) - hash + char
    hash |= 0
  }
  return hash.toString(36)
}

/** Schema 解析器单例 */
export const schemaParser = new SchemaParser()

export default schemaParser
