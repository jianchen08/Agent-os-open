/**
 * onboarding 内容/进度数据层 — onboarding_service 插件 http 面消费端。
 * 端点真值源 = 插件 plugin.json http_endpoints（生成物同步见
 * scripts/check_frontend_endpoints_sync.py）；此处用 extUrl 拼接。
 */

import apiClient from '@/services/api/client'
import { extUrl } from '@/services/api/extRoute'
import { contributionRegistry } from '@/services/schema/ContributionRegistry'
import { TOP_NAV_PANELS } from '@/services/workspacePanelOpener'
import type { OnboardingProgress, ProgressUpdate, Walkthrough } from './types'

export const ONBOARDING_PLUGIN_ID = 'onboarding_service'

export async function fetchOnboardingContent(): Promise<{ walkthroughs: Walkthrough[] }> {
  const res = await apiClient.get<{ walkthroughs: Walkthrough[] }>(
    extUrl(ONBOARDING_PLUGIN_ID, '/walkthroughs'),
  )
  return res.data
}

export async function fetchOnboardingProgress(): Promise<{ progress: OnboardingProgress }> {
  const res = await apiClient.get<{ progress: OnboardingProgress }>(
    extUrl(ONBOARDING_PLUGIN_ID, '/progress'),
  )
  return res.data
}

export async function postProgressUpdate(
  update: ProgressUpdate,
): Promise<{ progress: OnboardingProgress }> {
  const res = await apiClient.post<{ progress: OnboardingProgress }>(
    extUrl(ONBOARDING_PLUGIN_ID, '/progress'),
    update,
  )
  return res.data
}

/**
 * 面板路径 → 工作区页签 id（visitedPanels 判定用）。
 * 口径与 workspacePanelOpener 同源：内置面板查 TOP_NAV_PANELS，插件页查
 * contributes.pages 按 path 声明（openWorkspacePanelByPath 的两段解析）。
 */
export function panelPathToTabId(path: string): string | null {
  const spec = TOP_NAV_PANELS[path]
  if (spec) return spec.id
  const page = contributionRegistry.getPages().find((p) => p.path === path)
  return page ? `ws-plugin-${page.id}` : null
}
