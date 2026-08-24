/**
 * DSH 适配器前端服务（task_dsh_plugin_adapter 任务 2）。
 *
 * 职责（薄层，不引入运行时机制）：
 * 1. 从 /api/v1/schema 的 plugin_contributes 里找 dsh_adapter 插件的贡献块，
 *    读取来源版本记录（DSH commit/版本、通道、组件清单）；
 * 2. 把 contributes.renderers（tool → card）注册进 render 意图注册表——
 *    作为 render 描述符缺失时的兜底通道（正常路径：plugin.json 的
 *    capabilities.tools[].render 经 ToolDescriptor 直达 dshRenderIntent）；
 * 3. 失败隔离：单条 renderer 注册失败只 warn，不影响其他条目与主流程；
 *    适配器插件禁用 → schema 刷新 → 注册表清空，组件/工具一并下线。
 *
 * 组件本体不在此装载——vendor 组件（components/vendor/dsh/）静态编译进
 * 前端，由 ActivityCard 的 dsh:* 分支按 render 意图路由。
 */

import { getSchema } from '@/services/api/schema'
import { addRenderIntent, type ToolRenderIntent } from '@/utils/dshRenderIntent'
import { loggers } from '@/utils/logger'

/** dsh_adapter 插件 contributes.dsh_adapter 块（plugin.json 同构）。 */
export interface DshAdapterInfo {
  source_commit: string
  source_version: string
  backend_channel: string
  frontend_channel: string
  components: string[]
  out_of_scope: string[]
}

/** 单个 renderer 贡献（tool → render 卡）。 */
export interface DshRendererContribution {
  tool: string
  card: ToolRenderIntent['card']
}

/** 装载结果（供诊断/测试）。 */
export interface DshAdapterLoadResult {
  loaded: boolean
  renderersRegistered: number
  failures: string[]
  info: DshAdapterInfo | null
}

const VALID_CARDS: readonly ToolRenderIntent['card'][] = ['terminal', 'diff', 'read', 'web', 'search', 'generic']

/**
 * 拉取 schema 并装载 dsh_adapter 贡献。
 *
 * GrowthLoop 的 schema 刷新之后调用（render 描述符的主装载在
 * loadRenderIntents，这里只做 contributes 兜底 + 版本记录读取）。
 */
export async function loadDshAdapterContributions(): Promise<DshAdapterLoadResult> {
  const result: DshAdapterLoadResult = { loaded: false, renderersRegistered: 0, failures: [], info: null }
  let contributes: Array<Record<string, unknown>> = []
  try {
    const schema = await getSchema() as { plugin_contributes?: Array<Record<string, unknown>> }
    contributes = schema.plugin_contributes ?? []
  } catch (error) {
    loggers.websocket.warn('dshAdapter: schema 获取失败', error)
    result.failures.push(`schema: ${String(error)}`)
    return result
  }

  for (const contrib of contributes) {
    // plugin_contributes 条目形态：{ plugin_id, contributes }（内核透传）
    const inner = (contrib.contributes ?? contrib) as Record<string, unknown>
    const isAdapter =
      contrib.plugin_id === 'dsh_adapter' || (typeof inner.dsh_adapter === 'object' && inner.dsh_adapter !== null)
    if (!isAdapter) continue

    // 版本与来源记录
    if (inner.dsh_adapter && typeof inner.dsh_adapter === 'object') {
      result.info = inner.dsh_adapter as unknown as DshAdapterInfo
      result.loaded = true
    }

    // renderers 兜底注册（失败隔离）
    const renderers = Array.isArray(inner.renderers) ? inner.renderers : []
    for (const r of renderers) {
      try {
        const { tool, card } = r as DshRendererContribution
        if (typeof tool !== 'string' || !VALID_CARDS.includes(card)) {
          result.failures.push(`bad renderer entry: ${JSON.stringify(r)}`)
          continue
        }
        addRenderIntent(tool, { card })
        result.renderersRegistered += 1
      } catch (error) {
        result.failures.push(`renderer ${JSON.stringify(r)}: ${String(error)}`)
      }
    }
  }

  if (result.failures.length > 0) {
    loggers.websocket.warn('dshAdapter: 部分贡献装载失败（已隔离）', result.failures)
  }
  return result
}
