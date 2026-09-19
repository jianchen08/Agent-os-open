// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/** contextUsageStore getPromptTokens：未报告管道回退 0 */
import { beforeEach, describe, expect, it } from 'vitest'
import { useContextUsageStore } from '@/stores/contextUsageStore'

describe('contextUsageStore getPromptTokens', () => {
  beforeEach(() => {
    useContextUsageStore.setState({ usageByPipeline: {} })
  })

  it('未上报过的管道 → 0', () => {
    expect(useContextUsageStore.getState().getPromptTokens('pipe-none')).toBe(0)
  })

  it('已上报管道 → 取该管道 promptTokens', () => {
    useContextUsageStore.getState().updateUsage('pipe-1', { prompt_tokens: 123 })
    expect(useContextUsageStore.getState().getPromptTokens('pipe-1')).toBe(123)
  })
})
