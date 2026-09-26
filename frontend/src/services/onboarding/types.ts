/**
 * onboarding 域类型 — 与插件 content_schema.py 词表逐一对应
 * （docs/decisions/2026-09-24-onboarding-plugin.md；新增条件类型必须双侧同步）。
 */

export interface ApiCheckCondition {
  type: 'api_check'
  endpoint: string
  /** 点路径（如 "defaults.chat"）；缺省取响应根 */
  json_path?: string
  op?: 'non_empty'
  method?: 'GET'
}

export interface PanelVisitedCondition {
  type: 'panel_visited'
  panel: string
}

export interface ModeSelectedCondition {
  type: 'mode_selected'
  mode: string
}

export interface CtaClickedCondition {
  type: 'cta_clicked'
}

export interface ManualCondition {
  type: 'manual'
}

export interface CompositeCondition {
  type: 'all' | 'any'
  conditions: CompletionCondition[]
}

export type CompletionCondition =
  | ApiCheckCondition
  | PanelVisitedCondition
  | ModeSelectedCondition
  | CtaClickedCondition
  | ManualCondition
  | CompositeCondition

export interface CtaAction {
  type: 'open_panel' | 'switch_mode' | 'external_url' | 'focus_composer'
  target?: string
}

export interface CtaSpec {
  label: string
  action: CtaAction
}

export interface WalkthroughStep {
  id: string
  title: string
  body: string
  /** 内嵌向导键（注册表见插件 content_schema.WIZARD_REGISTRY） */
  wizard?: string
  cta?: CtaSpec
  completion?: CompletionCondition
  optional?: boolean
}

export interface Walkthrough {
  id: string
  title: string
  description: string
  order: number
  default_open: boolean
  steps: WalkthroughStep[]
}

/** 单条进度更新载荷（POST /ext/onboarding_service/progress） */
export interface ProgressUpdate {
  walkthrough_id: string
  step_id: string
  done: boolean
  how: string
}

export interface StepProgress {
  done: boolean
  done_at: number
  how: string
}

/** {[walkthroughId]: {[stepId]: StepProgress}} */
export type OnboardingProgress = Record<string, Record<string, StepProgress>>
