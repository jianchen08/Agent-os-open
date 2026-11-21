/** @feature FP-T12 前端组件补测 | @ci: frontend-test */
/**
 * SceneHistory 分组兜底分支：活动类别可成实例但实例列表缺失其归属时，
 * 行落「文件与其它动作」兜底组（不丢行、不建伪实例组）。
 */
import { fireEvent, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { renderWithProviders } from '@/test/renderWithProviders'
import { SceneHistory } from '../SceneHistory'
import { SceneInstanceCategoryBadge } from '../SceneInstanceBoard'
import type { SceneActivity } from '../sceneActivities'

function activity(partial: Partial<SceneActivity> & Pick<SceneActivity, 'callId' | 'category'>): SceneActivity {
  return {
    toolName: partial.toolName ?? partial.category,
    args: {},
    state: 'done',
    summary: partial.summary ?? `${partial.category} 动作`,
    ...partial,
  } as SceneActivity
}

describe('SceneHistory · 无实例归属的兜底分组', () => {
  it('实例列表缺归属 → 通道类活动落「文件与其它动作」组（不丢行）', () => {
    renderWithProviders(
      <SceneHistory
        activities={[
          activity({ callId: 't1', category: 'terminal', summary: '跑了一次命令' }),
          activity({ callId: 'f1', category: 'file', summary: '写了一个文件' }),
        ]}
        instances={[]}
      />,
    )

    fireEvent.click(screen.getByTestId('task-scene-history-toggle'))

    const loose = screen
      .getAllByTestId('scene-history-group')
      .find((el) => el.getAttribute('data-group-key') === '__unattached__')
    expect(loose).toBeDefined()
    expect(loose?.getAttribute('data-group-count')).toBe('2')
    expect(loose).toHaveTextContent('文件与其它动作')
    // 组内行默认收起；计数=2 已证明两条活动都落兜底组
  })
})

describe('SceneInstanceCategoryBadge · 组头徽标', () => {
  it('按类别取图标渲染（历史区组头复用面）', () => {
    const { container } = renderWithProviders(
      <SceneInstanceCategoryBadge category="terminal" />,
    )
    expect(container.querySelector('svg')).not.toBeNull()
  })
})
