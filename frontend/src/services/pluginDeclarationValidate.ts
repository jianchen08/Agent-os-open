/**
 * 插件声明合法性校验（Phase 1-C5，对标主题 validateThemeConfig）。
 *
 * 背景：前端对畸形声明的默认态度是"逐点兜底不崩"（normalizeRenderIntent 弃坏卡、
 * SchemaRouter 落回默认空间、toRjsf 未知 type→string）——这保证不白屏，但坏声明
 * **静默吞掉**，开发期完全不可见。本校验器在 `reloadContributionRegistry` 装载时
 * 对页面 / 工具渲染 / chat_card / ui_schema.widgets 声明做合法性与可渲染性检查，
 * 把问题收集上报（不再是静默降级）。
 *
 * 契约（校准自真实语料）：
 * - **error**：确定性会坏/丢内容的结构错误（缺 id、page.space 不在封闭空间集、
 *   字段缺 name、字段 type 不在词汇表、widget 缺 id/type、chat_card 块 type 非法）；
 * - **warning**：能优雅降级但应暴露的（未知 widget space → 已落回默认空间、
 *   render.card 缺 kind 等）。
 *
 * 校验器不得空转：它必须在真实数据上抓到错误（语料测试）且对故意坏输入报错
 * （负例测试）——见 pluginDeclarationValidate.test.ts。
 */

/** 输入：从聚合 schema（或原始 plugin.json）抽取的声明面。 */
export interface PluginDeclarationInput {
  /** 页面声明（contributes.pages[].schema[].fields 场为 UIInputFormField 词汇） */
  pages?: Array<Record<string, unknown>>
  /** 工具声明（tools[].render / tools[].ui.chat_card / tools[].ui.interaction_modes） */
  tools?: Array<Record<string, unknown>>
  /** widget 声明（ui_schema.widgets[]） */
  uiSchemaWidgets?: Array<Record<string, unknown>>
  /** 流式能力声明（capabilities.streaming，协议见 docs/streaming-protocol.md） */
  streaming?: Record<string, unknown>
  /**
   * 本插件是否为 external MCP（plugin.json entry === "mcp:external"）。
   * 聚合 schema 是跨插件平铺的 tools 数组、无法逐工具追溯 entry，此标记由
   * 逐插件扫描侧（真实语料测试/未来 schema 细分）显式传入。
   */
  externalMcp?: boolean
}

/** 流式协议事件清单（与 config/kernel_capabilities/streaming.json 的 10 个
 * capability method 一致；单一真值源在 JSON，此处仅人读速览——机械一致性由
 * 内核加载器结构校验兜底（事件不在契约内网关 fail-closed 拒收）） */
const STREAMING_EVENTS = new Set([
  'stream_start', 'stream_chunk', 'thinking_start', 'thinking_chunk', 'thinking_end',
  'tool_start', 'tool_result', 'new_message', 'stream_end', 'stream_error',
])

export interface PluginValidationResult {
  errors: string[]
  warnings: string[]
  get valid(): boolean
}

/** 页面目标空间（与 ContributionRegistry.PageSpace 一致，封闭集合） */
const PAGE_SPACES = new Set(['settings', 'workspace', 'chat', 'floating', 'dock', 'fullscreen'])

/** 字段 type 词汇表（与 types/schema.ts UIInputFormField.type 一致） */
const FIELD_TYPES = new Set([
  'string', 'number', 'boolean', 'select', 'multiselect', 'textarea', 'date', 'file',
  'input', 'toggle', 'slider', 'color', 'radio', 'checkbox',
])

/** chat_card 块类型（与 utils/chatCardInterpreter.ts ChatCardBlockDecl.type 一致，
 * 经真实语料校准——首版只写了 text/kv/form，在 builtin_tools 的
 * code/diff 块上误报，修正为完整集合） */
const CHAT_CARD_BLOCK_TYPES = new Set([
  'text', 'code', 'json', 'markdown', 'diff', 'kv', 'file', 'image', 'link', 'log', 'form',
])

function nonEmptyString(v: unknown): boolean {
  return typeof v === 'string' && v.trim().length > 0
}

/** 校验一个 UIInputFormField 形状（page.schema[].fields[] / chat_card form.fields[]） */
function validateFields(fields: unknown, ctx: string, errs: string[], warns: string[]): void {
  if (fields === undefined || fields === null) return
  if (!Array.isArray(fields)) {
    errs.push(`${ctx}.fields 应为数组`)
    return
  }
  for (let i = 0; i < fields.length; i++) {
    const f = fields[i]
    if (!f || typeof f !== 'object') {
      errs.push(`${ctx}.fields[${i}] 不是对象`)
      continue
    }
    const rec = f as Record<string, unknown>
    const name = rec.name
    if (!nonEmptyString(name)) errs.push(`${ctx}.fields[${i}] 缺 name（该条会被渲染端整条丢弃）`)
    const type = rec.type
    if (type !== undefined && typeof type === 'string' && !FIELD_TYPES.has(type)) {
      // 未知 type 渲染端兜底为 string——不崩但形状失实，记 warning
      warns.push(`${ctx}.fields[${i}] name=${String(name)} 未知 type=${type}（将落回 string 渲染）`)
    }
    // select/multiselect/radio/checkbox 下拉型字段缺 options 且无动态源 → 空下拉
    if (typeof type === 'string' && ['select', 'multiselect', 'radio', 'checkbox'].includes(type)) {
      const hasOptions = Array.isArray(rec.options) && rec.options.length > 0
      const hasDatasource = nonEmptyString(rec.datasourceUri)
      if (!hasOptions && !hasDatasource) {
        warns.push(`${ctx}.fields[${i}] name=${String(name)} type=${type} 缺 options/datasourceUri（下拉为空）`)
      }
    }
    // 动态源模板配平（datasourceUri 含 {{字段}} / source 含 {{表达式}} 未闭合 → 渲染空）
    for (const key of ['datasourceUri', 'source', 'defaultSource'] as const) {
      const raw = rec[key]
      if (typeof raw !== 'string') continue
      const open = (raw.match(/\{\{/g) ?? []).length
      const close = (raw.match(/\}\}/g) ?? []).length
      if (open !== close) {
        warns.push(`${ctx}.fields[${i}] name=${String(name)} ${key} 模板括号未配平（渲染空值）`)
      }
    }
  }
}

function validateChatCard(cc: Record<string, unknown>, ctx: string, errs: string[], warns: string[]): void {
  if (cc.title !== undefined && !nonEmptyString(cc.title)) warns.push(`${ctx}：title 非字符串`)
  const blocks = cc.blocks
  if (blocks === undefined) return
  if (!Array.isArray(blocks)) {
    errs.push(`${ctx}.blocks 应为数组`)
    return
  }
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i]
    if (!b || typeof b !== 'object') {
      errs.push(`${ctx}.blocks[${i}] 不是对象`)
      continue
    }
    const type = (b as Record<string, unknown>).type
    if (typeof type !== 'string' || !CHAT_CARD_BLOCK_TYPES.has(type)) {
      errs.push(`${ctx}.blocks[${i}] 非法块 type=${String(type)}（该块会被跳过，内容丢失）`)
      continue
    }
    // text 块至少要有 label/source 之一，否则渲染空
    if (type === 'text') {
      const has = nonEmptyString((b as Record<string, unknown>).label) || nonEmptyString((b as Record<string, unknown>).source)
      if (!has) warns.push(`${ctx}.blocks[${i}] text 块缺 label/source（渲染为空）`)
    }
    // 块级 source 模板配平（{{...}} 未闭合 → 该块渲染空值）
    const src = (b as Record<string, unknown>).source
    if (typeof src === 'string') {
      const open = (src.match(/\{\{/g) ?? []).length
      const close = (src.match(/\}\}/g) ?? []).length
      if (open !== close) warns.push(`${ctx}.blocks[${i}] source 模板括号未配平（渲染空值）`)
    }
    if (type === 'kv') {
      const kvFields = (b as Record<string, unknown>).fields
      if (!Array.isArray(kvFields)) warns.push(`${ctx}.blocks[${i}] kv 块缺 fields`)
    }
    if (type === 'form') {
      const form = (b as { form?: { fields?: unknown } }).form
      validateFields(form?.fields, `${ctx}.blocks[${i}].form`, errs, warns)
    }
    // 内容型块（code/diff/json/markdown/file/image/link/log）至少要有 content/source 之一
    if (['code', 'json', 'markdown', 'file', 'image', 'link', 'log'].includes(type)) {
      const r = b as Record<string, unknown>
      if (!nonEmptyString(r.content) && !nonEmptyString(r.source) && !Array.isArray(r.diffOld)) {
        warns.push(`${ctx}.blocks[${i}] ${type} 块缺 content/source（渲染为空）`)
      }
    }
  }
}

/** 校验单条 tool 声明的渲染面（render / ui.chat_card / interaction_modes / view_modes）。 */
function validateTool(tool: Record<string, unknown>, errs: string[], warns: string[]): void {
  const name = tool.name
  const ctx = `tools[${nonEmptyString(name) ? `name=${String(name)}` : '?name'}]`
  if (!nonEmptyString(name)) errs.push(`${ctx} 缺 name`)
  const render = tool.render
  if (render !== undefined && render !== null) {
    if (typeof render !== 'object') {
      warns.push(`${ctx}.render 非对象（渲染声明被弃）`)
    } else if (typeof (render as Record<string, unknown>).card === 'object') {
      const card = (render as { card?: { kind?: unknown } }).card
      if (!nonEmptyString(card?.kind)) {
        warns.push(`${ctx}.render.card 缺 kind（声明被弃，落数据形状路由）`)
      }
    }
  }
  const ui = tool.ui
  if (ui && typeof ui === 'object') {
    const cc = (ui as Record<string, unknown>).chat_card
    if (cc !== undefined && cc !== null) {
      if (typeof cc !== 'object') errs.push(`${ctx}.ui.chat_card 非对象`)
      else validateChatCard(cc as Record<string, unknown>, ctx, errs, warns)
    }
    for (const facet of ['interaction_modes', 'view_modes'] as const) {
      const v = (ui as Record<string, unknown>)[facet]
      if (v !== undefined && v !== null && typeof v !== 'object') {
        warns.push(`${ctx}.ui.${facet} 非对象（该声明被弃）`)
      }
    }
  }
}

/**
 * 主入口：校验插件声明面，收集问题（不抛异常、不阻断；消费方决定展示/日志）。
 */
export function validatePluginDeclaration(
  input: PluginDeclarationInput | undefined,
): PluginValidationResult {
  const errors: string[] = []
  const warnings: string[] = []
  if (!input) return { errors, warnings, get valid() { return errors.length === 0 } }

  for (const [i, page] of (input.pages ?? []).entries()) {
    const ctx = `pages[${i}] id=${nonEmptyString(page?.id) ? String(page.id) : '?'}`
    if (!nonEmptyString(page?.id)) errors.push(`${ctx} 缺 id（贡献点无法注册）`)
    const space = page?.space
    if (!nonEmptyString(space)) errors.push(`${ctx} 缺 space（目标空间非法）`)
    else if (!PAGE_SPACES.has(String(space))) errors.push(`${ctx} 未知 space=${String(space)}（非封闭空间集，页面无法挂载）`)
    const schema = page?.schema
    if (schema !== undefined && schema !== null) {
      if (typeof schema !== 'object') warnings.push(`${ctx}.schema 非对象`)
      else validateFields((schema as { fields?: unknown }).fields, `${ctx}.schema`, errors, warnings)
    }
  }

  for (const tool of input.tools ?? []) {
    if (tool && typeof tool === 'object') {
      const rec = tool as Record<string, unknown>
      // 强制规则：external MCP 工具必须声明 input_schema。
      // manifest 声明是 LLM 工具面参数 schema 的唯一真值源（G2 只比对不回填
      // 握手 schema），缺声明 = 内核补注册 {} = LLM 收到零参数工具 → 调用必因
      // 缺参被服务端校验拒绝（omnisearch universal_search 缺 mode 100% 失败、
      // 调研 agent 空转 45 万 token 的根因）。前端校验器与内核注册闸（
      // plugin_lifecycle.rs 拒注册）双端对齐，此规则抓声明侧提前暴露。
      if (input.externalMcp && !rec.input_schema) {
        const name = nonEmptyString(rec.name) ? String(rec.name) : '?'
        errors.push(
          `tools[name=${name}] external MCP 工具缺 input_schema（声明是 LLM 工具面唯一真源，缺失=内核注册 {} + 零参数盲调，内核将拒绝注册；请按 MCP tools/list inputSchema 补齐）`,
        )
      }
      validateTool(rec, errors, warnings)
    } else warnings.push('tools 有非对象条目')
  }

  for (const [i, w] of (input.uiSchemaWidgets ?? []).entries()) {
    const ctx = `ui_schema.widgets[${i}]`
    if (!w || typeof w !== 'object') {
      errors.push(`${ctx} 非对象`)
      continue
    }
    if (!nonEmptyString(w.id)) errors.push(`${ctx} 缺 id`)
    if (!nonEmptyString(w.type)) errors.push(`${ctx} 缺 type（无法路由到 widget 渲染）`)
    // 自定义 space 属合法扩展（widget_demo 用 widget-demo/agent-studio 等自定义空间），
    // 未知时 SchemaRouter 落回默认空间——记 warning 不报 error
    if (w.space !== undefined && !nonEmptyString(w.space)) {
      warnings.push(`${ctx} id=${String(w.id)} space 非字符串`)
    }
    const wProps = (w as { props?: { fields?: unknown } }).props
    validateFields(wProps?.fields, `${ctx}.props`, errors, warnings)
  }

  validateStreaming(input.streaming, errors, warnings)

  return { errors, warnings, get valid() { return errors.length === 0 } }
}

/** 校验 capabilities.streaming 声明（流式协议，docs/streaming-protocol.md）。
 * 错误 = 必然被内核网关拒绝/前端渲染失效的声明；warning = 能工作但应暴露的。 */
function validateStreaming(
  streaming: Record<string, unknown> | undefined,
  errs: string[],
  warns: string[],
): void {
  if (streaming === undefined || streaming === null) return
  if (typeof streaming !== 'object') {
    errs.push('capabilities.streaming 非对象')
    return
  }
  const events = streaming.events
  if (events !== undefined) {
    if (!Array.isArray(events)) {
      errs.push('capabilities.streaming.events 应为数组')
    } else {
      for (const [i, e] of events.entries()) {
        if (typeof e !== 'string' || !STREAMING_EVENTS.has(e)) {
          // 事件不在契约清单内 → 网关按 streaming.json 找不到 spec 会透传，
          // 但前端无 handler 消费（事件静默丢弃）——声明失实，报 error
          errs.push(`capabilities.streaming.events[${i}] 未知事件 ${String(e)}（不在流式契约 10 事件内，发射即被前端丢弃）`)
        }
      }
    }
  }
  const partTypes = streaming.part_types
  if (partTypes !== undefined) {
    if (!Array.isArray(partTypes)) {
      errs.push('capabilities.streaming.part_types 应为数组')
    } else {
      for (const [i, pt] of partTypes.entries()) {
        if (!nonEmptyString(pt)) {
          errs.push(`capabilities.streaming.part_types[${i}] 非字符串`)
        } else if (!/^[0-9a-z_]{1,32}$/.test(String(pt))) {
          warns.push(`capabilities.streaming.part_types[${i}] ${String(pt)} 含非法字符（前端注册键须与声明完全一致）`)
        }
      }
    }
  }
  if (streaming.persist !== undefined && typeof streaming.persist !== 'boolean') {
    errs.push('capabilities.streaming.persist 应为布尔')
  }
}
