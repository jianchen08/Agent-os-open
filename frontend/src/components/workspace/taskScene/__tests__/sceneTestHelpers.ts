/** @feature FP-0.2.四 前端Schema（任务现场） @vision V6 可即用 @ci frontend-test */
/**
 * 现场测试共用动作：默认视图是实例看板，逐条调用在「活动历史」里。
 *
 * 断言流水行/终端块/筛选条之前必须先展开历史区与目标分组——这是新结构的
 * 真实交互路径（不是测试绕过），与用户点开复盘的操作一致。
 */
import { screen, within } from '@testing-library/react'
import type { UserEvent } from '@testing-library/user-event'

/** 展开活动历史区（若尚未展开） */
export async function expandHistory(user: UserEvent): Promise<void> {
  const toggle = screen.getByTestId('task-scene-history-toggle')
  if (toggle.getAttribute('aria-expanded') !== 'true') await user.click(toggle)
}

/** 展开历史区下所有实例分组 */
export async function expandAllGroups(user: UserEvent): Promise<void> {
  await expandHistory(user)
  for (const toggle of screen.queryAllByTestId('scene-history-group-toggle')) {
    if (toggle.getAttribute('aria-expanded') !== 'true') await user.click(toggle)
  }
}

/** 取某分组容器（按实例键） */
export function groupByKey(key: string): HTMLElement {
  const group = screen
    .getAllByTestId('scene-history-group')
    .find((el) => el.getAttribute('data-group-key') === key)
  if (!group) throw new Error(`history group not found: ${key}`)
  return group
}

/** 组内全部流水行 */
export function rowsIn(group: HTMLElement): HTMLElement[] {
  return within(group).queryAllByTestId('scene-row')
}
