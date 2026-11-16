/**
 * onboarding 自动引导（净装态 key 注入缝的呈现层，§5.2 配置缝）。
 *
 * 触发驱动 = 「configure_llm 步骤未完成」（用户裁定 2026-10-24：任何时候新装
 * 都应弹出引导）：.env 无任何 *_API_KEY 且持久化进度未 done 且 evaluator 求值
 * 未满足 → 打开引导页签并直达 llm_setup 向导第 1 步（选供应商+填 Key，写入与
 * 设置页同一内核配置面）。节流 = sessionStorage 会话旗标：每次应用启动至多
 * 自动弹一次，下次启动仍未完成 → 再弹；不再用持久 localStorage 一票否决。
 * 已存在 key = 已配置（现状口径，不引入 key 有效性校验）→ 本启动不打扰。
 * 插件未装载/取数失败 → 静默降级：不弹、不抛、不落旗标（下次 schema 事件重试）。
 */

import apiClient from '@/services/api/client'
import { getUserEnvVarSet } from '@/services/api/config'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { openWorkspacePanelByPath } from '@/services/workspacePanelOpener'
import { fetchOnboardingContent, fetchOnboardingProgress } from './content'
import { collectApiChecks, evaluateCondition } from './evaluator'
import { requestLlmWizardFocus } from './llmWizardFocus'
import type { EvaluationContext } from './evaluator'
import type {
  CompletionCondition,
  OnboardingProgress,
  Walkthrough,
  WalkthroughStep,
} from './types'

/** onboarding「开始使用」页（onboarding_service contributes.pages 声明路径） */
export const ONBOARDING_PAGE_PATH = '/p/get_started'

/** 会话旗标（sessionStorage；每次应用启动新会话，schema 事件重触发靠它去重） */
const SESSION_FLAG = 'lingxi.onboarding.autoopen.session.v2'

/** 依赖注入面（测试替身挂点；生产用默认实现） */
export interface AutoOpenDeps {
  getPagePaths: () => string[]
  getEnvVarSet: () => Promise<Set<string>>
  fetchContent: () => Promise<{ walkthroughs: Walkthrough[] }>
  fetchProgress: () => Promise<{ progress: OnboardingProgress }>
  /** api_check 端点取数（失败 = 该端点条件不满足，与 evaluator 语义一致） */
  fetchApiResult: (endpoint: string) => Promise<unknown>
  openByPath: (path: string) => boolean
  requestFocus: () => void
}

const defaultDeps: AutoOpenDeps = {
  getPagePaths: () => contributionRegistry.getPages().map((p) => p.path ?? ''),
  getEnvVarSet: getUserEnvVarSet,
  fetchContent: fetchOnboardingContent,
  fetchProgress: fetchOnboardingProgress,
  fetchApiResult: async (endpoint) => (await apiClient.get<unknown>(endpoint)).data,
  openByPath: openWorkspacePanelByPath,
  requestFocus: requestLlmWizardFocus,
}

function hasSessionFlag(): boolean {
  try {
    return sessionStorage.getItem(SESSION_FLAG) !== null
  } catch {
    return false
  }
}

/** 落会话旗标（存储不可用=同启动重复判定多走几次取数，行为仍正确） */
function setSessionFlag(): void {
  try {
    sessionStorage.setItem(SESSION_FLAG, 'done')
  } catch {
    // sessionStorage 禁用态：不落旗标，判定链每 Schema 事件重走（幂等）
  }
}

/** 定位内容中承载 llm_setup 向导的步骤（触发锚：configure_llm） */
export function findLlmSetupStep(
  walkthroughs: Walkthrough[],
): { walkthrough: Walkthrough; step: WalkthroughStep } | null {
  for (const w of walkthroughs) {
    const step = w.steps.find((s) => s.wizard === 'llm_setup')
    if (step) return { walkthrough: w, step }
  }
  return null
}

/** 组装求值上下文：api_check 端点各取数一次，失败按缺席处理（不满足） */
async function buildEvaluationContext(
  cond: CompletionCondition,
  fetchApiResult: (endpoint: string) => Promise<unknown>,
): Promise<EvaluationContext> {
  const endpoints = [...new Set(collectApiChecks(cond).map((c) => c.endpoint))]
  const results = await Promise.all(
    endpoints.map(async (endpoint) => {
      try {
        return [endpoint, await fetchApiResult(endpoint)] as const
      } catch {
        return [endpoint, undefined] as const
      }
    }),
  )
  return {
    apiResults: new Map(results),
    visitedPanels: new Set(),
    selectedModes: new Set(),
    ctaClickedSteps: new Set(),
  }
}

/**
 * 启动判定：会话旗标未落 + 页面已声明 + .env 无 key + configure_llm 未完成
 * （持久化进度与 evaluator 双判）→ 打开引导页签并请求直达向导第 1 步。
 * 返回是否执行了打开（诊断/测试用）。任何取数失败静默降级返回 false
 * （不落旗标，下次 schema 事件重试）。
 */
export async function maybeAutoOpenOnboarding(deps: AutoOpenDeps = defaultDeps): Promise<boolean> {
  try {
    if (hasSessionFlag()) return false
    if (!deps.getPagePaths().includes(ONBOARDING_PAGE_PATH)) return false
    const envVars = await deps.getEnvVarSet()
    if ([...envVars].some((name) => name.endsWith('_API_KEY'))) {
      setSessionFlag() // 已配置：本启动不再打扰
      return false
    }
    const [{ walkthroughs }, { progress }] = await Promise.all([
      deps.fetchContent(),
      deps.fetchProgress(),
    ])
    const found = findLlmSetupStep(walkthroughs)
    if (!found) return false
    if (progress[found.walkthrough.id]?.[found.step.id]?.done === true) {
      setSessionFlag() // 已完成：本启动不再打扰
      return false
    }
    if (found.step.completion) {
      const key = `${found.walkthrough.id}/${found.step.id}`
      const ctx = await buildEvaluationContext(found.step.completion, deps.fetchApiResult)
      if (evaluateCondition(found.step.completion, ctx, key)) {
        setSessionFlag() // 条件已满足（如 defaults.chat 已配）：本启动不再打扰
        return false
      }
    }
    setSessionFlag()
    deps.requestFocus()
    return deps.openByPath(ONBOARDING_PAGE_PATH)
  } catch {
    return false // 取数失败静默降级：不落旗标，下次 schema 事件重试
  }
}
