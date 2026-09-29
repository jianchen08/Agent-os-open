/**
 * 模式声明面 registry 消费（GET /ext/agent_manager/modes，2026-09-28 设计 D5）
 *
 * mode.yaml 是模式声明单一真值（agent_manager 插件聚合 + schema 校验），
 * 前端所有模式声明取数（专属管道 / 面板页 / 呈现来源 / 工具卡片策略）
 * 单点走本 registry，禁止各处硬编码模式键映射。
 */

import { useQuery } from '@tanstack/react-query'
import { apiClient } from '@/services/api/client'
import { queryKeys } from '@/services/query/queryKeys'

/** 呈现来源声明（mode.yaml presenter.source） */
export type ModePresenterSource = 'data_cards' | 'agent_registry' | 'none'

/** 工具卡片策略声明（mode.yaml tool_card） */
export type ModeToolCard = 'native' | 'collapse' | 'hide'

/** 管道消费情境（mode.yaml pipelines[].context，D2 多管列表化）：
 *  conversation=对话链（会话路由）/ task=任务链（派发路由） */
export type ModePipelineContext = 'conversation' | 'task'

/** 专属管道声明条目（一管一配置：name 为 config/pipelines/ 登记名，模式只持引用） */
export interface ModePipelineDecl {
  name: string
  context: ModePipelineContext
}

/** 模式声明条目（/ext/agent_manager/modes 响应子集；缺省语义由服务端补齐） */
export interface ModeDeclaration {
  mode: string
  name: string
  description?: string | null
  /** 专属管道列表（D2 多管声明）；空 = 缺省共享 autonomous */
  pipelines: ModePipelineDecl[]
  panel_page_id?: string | null
  presenter: { source: ModePresenterSource }
  tool_card: ModeToolCard
  /** 提示词物料组装器（material.py::build_injection 形态；null=无组装器） */
  material?: string | null
  /** 选择器选项图标（mode.yaml icon，emoji；未声明 = null） */
  icon?: string | null
  /** 模式主题 id（mode.yaml theme；未声明 = null = 不切换主题） */
  theme?: string | null
  /** 人设接管声明（mode.yaml persona；未声明 = null = 不接管） */
  persona?: { replace: boolean; from: string } | null
  /** 模式包目录名（mode_X，/ext 数据端点前缀构成成分） */
  plugin_id: string
}

/** modes registry 响应（errors = 校验失败包的原因清单，fail-closed 不带病透出） */
export interface ModesRegistryResponse {
  modes: ModeDeclaration[]
  total: number
  errors: string[]
}

/** 声明 → 呈现数据端点；source 非 data_cards 无插件数据端点，返回 null */
export function modePresenterEndpointOf(decl: ModeDeclaration): string | null {
  if (decl.presenter.source !== 'data_cards') return null
  return `/ext/${decl.plugin_id}/data/cards`
}

/** registry 响应 → {模式包前缀 → 呈现数据端点}（呈现档案数据源映射，声明派生） */
export function presenterSourcesFromModes(
  res: ModesRegistryResponse | undefined,
): Record<string, string> {
  const out: Record<string, string> = {}
  for (const decl of res?.modes ?? []) {
    const endpoint = modePresenterEndpointOf(decl)
    if (endpoint) out[decl.plugin_id] = endpoint
  }
  return out
}

/** 任务模式选择器选项形态（form select options 条目契约：label/value/icon/description） */
export interface TaskModeOption {
  label: string
  value: string
  icon?: string
  description?: string
}

/**
 * registry → 任务模式选择器模式项（D1 标签裁定：选项=registry 全量派生，
 * 「默认」兜底档由选择器本体声明持有，不在本函数产物内）。逐模式派生
 * label=decl.name、value=decl.mode 键、icon/description 随声明（缺省不带键）。
 * registry 不可达/未就绪（undefined/空 modes）→ 空数组（选择器只剩「默认」，
 * 装饰性数据不报错阻断）。
 */
export function taskModeOptionsFromModes(
  res: ModesRegistryResponse | undefined,
): TaskModeOption[] {
  return (res?.modes ?? []).map((decl) => ({
    label: decl.name,
    value: decl.mode,
    ...(typeof decl.icon === 'string' && decl.icon ? { icon: decl.icon } : {}),
    ...(typeof decl.description === 'string' && decl.description
      ? { description: decl.description }
      : {}),
  }))
}

/** registry 变化频率极低（模式包增删/热发现才变），新鲜窗口 5 分钟 */
const MODES_STALE_TIME = 5 * 60_000

/** registry 拉取（hook 与非组件方 fetchQuery 共用同一 queryFn） */
export async function fetchModesRegistry(): Promise<ModesRegistryResponse> {
  const res = await apiClient.get<ModesRegistryResponse>('/ext/agent_manager/modes')
  return res.data
}

/** 模式声明面 registry（enabled=false 零请求；装饰性数据失败不轰打，不重试） */
export function useModesRegistry(enabled = true): ModesRegistryResponse | undefined {
  const query = useQuery({
    queryKey: queryKeys.modesRegistry,
    queryFn: fetchModesRegistry,
    enabled,
    staleTime: MODES_STALE_TIME,
    retry: false,
  })
  return query.data
}
