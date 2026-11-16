/**
 * 模式声明面 registry 消费（GET /ext/agent_manager/modes，2026-09-28 设计 D5）
 *
 * mode.yaml 是模式声明单一真值（agent_manager 插件聚合 + schema 校验），
 * 前端所有模式声明取数（专属管道 / 面板页 / 呈现来源 / 工具卡片策略）
 * 单点走本 registry，禁止各处硬编码模式键映射。
 */

import { useQuery } from '@tanstack/react-query'
import { AGENT_MANAGER_ENDPOINTS, MODE_DATA_CARDS_ENDPOINTS } from '@/services/api/endpoints.generated'
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
  /** 方案规划段领域规格（mode.yaml plan_spec，技能名；未声明 = null = 无规格，
   *  骨架第零步注册表查询消费，批 B2） */
  plan_spec?: string | null
  /** 规划期可派发执行者（mode.yaml planning_executors；未声明 = null = 零贡献，
   *  task_submit plan 态闸聚合消费，批 B4） */
  planning_executors?: string[] | null
  /** 人设接管声明（mode.yaml persona；未声明 = null = 不接管） */
  persona?: { replace: boolean; from: string } | null
  /** 出生默认模式标志（mode.yaml default；M1 并入刀，至多一包为 true——后端
   *  声明校验执法多标整批复决；false/缺省 = 非默认） */
  default?: boolean
  /** 前台可选声明（mode.yaml foreground_selectable；R10③ 补充裁定 2026-10-14）：
   *  false = 不出现在前台模式选择面选项（默认模式是兜底无需选、自进化等后台型
   *  模式不在前台选）；缺省 = 可选（向后兼容既有声明），显式声明才隐藏 */
  foreground_selectable?: boolean
  /** 模式包目录名（mode_X，/ext 数据端点前缀构成成分） */
  plugin_id: string
}

/** modes registry 响应（errors = 校验失败包的原因清单，fail-closed 不带病透出） */
export interface ModesRegistryResponse {
  modes: ModeDeclaration[]
  total: number
  errors: string[]
}

/** 声明 → 呈现数据端点；source 非 data_cards 或插件未声明该端点 = 无数据面，返回 null */
export function modePresenterEndpointOf(decl: ModeDeclaration): string | null {
  if (decl.presenter.source !== 'data_cards') return null
  return MODE_DATA_CARDS_ENDPOINTS[decl.plugin_id] ?? null
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
 * 声明 description → 选择器 UI 文案。mode.yaml 的 description 契约是主 agent
 * 域路由知识（首句=域定义，其后=直派/编排链要点），选择菜单只取首句域定义；
 * 无句号分隔（非同构声明）回退全文；未声明 → 无键。
 */
function modeSelectorDescriptionOf(decl: ModeDeclaration): { description?: string } {
  if (typeof decl.description !== 'string' || !decl.description) return {}
  const [first] = decl.description.split('。', 1)
  const text = (first ?? '').trim() || decl.description
  return { description: text }
}

/**
 * registry → 任务模式选择器模式项。逐模式派生 label=decl.name、value=decl.mode
 * 键、icon 随声明、description=声明首句（路由知识全文不进选择菜单，见
 * modeSelectorDescriptionOf；缺省不带键）。
 *
 * 声明式前台可选过滤（R10③ 补充裁定 2026-10-14）：`foreground_selectable: false`
 * 的模式不进选项（默认模式=兜底无需选、自进化等后台型模式不在前台选）；
 * 字段缺省 = 可选（向后兼容既有声明），显式声明才隐藏。
 * registry 不可达/未就绪（undefined/空 modes）→ 空数组（选择器只剩「默认」，
 * 装饰性数据不报错阻断）。
 */
export function taskModeOptionsFromModes(
  res: ModesRegistryResponse | undefined,
): TaskModeOption[] {
  return (res?.modes ?? [])
    .filter((decl) => decl.foreground_selectable !== false)
    .map((decl) => ({
      label: decl.name,
      value: decl.mode,
      ...(typeof decl.icon === 'string' && decl.icon ? { icon: decl.icon } : {}),
      ...modeSelectorDescriptionOf(decl),
    }))
}

/** registry 变化频率极低（模式包增删/热发现才变），新鲜窗口 5 分钟 */
const MODES_STALE_TIME = 5 * 60_000

/** registry 拉取（hook 与非组件方 fetchQuery 共用同一 queryFn） */
export async function fetchModesRegistry(): Promise<ModesRegistryResponse> {
  const res = await apiClient.get<ModesRegistryResponse>(AGENT_MANAGER_ENDPOINTS.agent_manager_modes)
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
