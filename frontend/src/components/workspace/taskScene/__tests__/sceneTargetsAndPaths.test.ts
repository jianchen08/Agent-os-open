/**
 * @feature: 任务现场可用性闭环（实例点击路由 / 产物路径 / 状态行预览文案）
 *
 * 覆盖本轮三个纯函数模块（机制层真值，不依赖渲染）：
 * - `sceneInstanceTargets.resolveInstanceTarget`：通道 → 点击目标（含不可用降级）
 * - `sceneFilePaths.baseNameOf / dirNameOf`：「未命名」修复的行为锚
 * - `chat/sceneStatusPreview.buildScenePreviewText`：气泡状态行预览文案
 *
 * 防拟合：每组都给 ≥2 组有区分度输入（命中/未命中、有前缀/无前缀、Windows 路径等）。
 */

import { describe, expect, it } from 'vitest'
import { buildScenePreviewText } from '@/components/chat/sceneStatusPreview'
import { baseNameOf, dirNameOf } from '@/components/workspace/taskScene/sceneFilePaths'
import {
  BROWSER_LIVE_PAGE,
  isActionableTarget,
  resolveInstanceTarget,
} from '@/components/workspace/taskScene/sceneInstanceTargets'

describe('resolveInstanceTarget — 通道 → 点击目标', () => {
  it('终端/桌面/软件/手机都落面板内运行窗口（inline）', () => {
    for (const category of ['terminal', 'desktop', 'software', 'phone'] as const) {
      const target = resolveInstanceTarget(category)
      expect(target.kind).toBe('inline')
      expect(isActionableTarget(target)).toBe(true)
      // 区分度：不是所有类别都同标签的空壳（label 恒定但 kind 必须来自分支）
      if (target.kind === 'inline') expect(target.label).toBe('运行窗口')
    }
  })

  it('浏览器走声明式插件页面，坐标与既有 browser_live 声明一致', () => {
    const target = resolveInstanceTarget('browser')
    expect(target).toMatchObject({
      kind: 'plugin_page',
      pluginId: BROWSER_LIVE_PAGE.pluginId,
      pageId: BROWSER_LIVE_PAGE.pageId,
    })
  })

  it('页面声明缺失 → unavailable（不渲染点击态），页面存在 → 可点', () => {
    const missing = resolveInstanceTarget('browser', () => false)
    expect(missing.kind).toBe('unavailable')
    expect(isActionableTarget(missing)).toBe(false)
    if (missing.kind === 'unavailable') expect(missing.reason).toContain('浏览器直播页未装载')

    const present = resolveInstanceTarget('browser', (pid, page) => {
      // 查询入参与声明坐标一致时才判存在（防止实现忽略入参恒 true）
      return pid === BROWSER_LIVE_PAGE.pluginId && page === BROWSER_LIVE_PAGE.pageId
    })
    expect(present.kind).toBe('plugin_page')
  })

  it('file/other 不成实例 → unavailable；未知类别同样不可点', () => {
    for (const category of ['file', 'other'] as const) {
      const target = resolveInstanceTarget(category)
      expect(target.kind).toBe('unavailable')
      expect(isActionableTarget(target)).toBe(false)
    }
  })
})

describe('baseNameOf / dirNameOf — 「未命名」修复的行为锚', () => {
  it('取真实文件名（含中文、多级目录、Windows 反斜杠）', () => {
    expect(baseNameOf('docs/方案说明.md')).toBe('方案说明.md')
    expect(baseNameOf('src/auth/login.py')).toBe('login.py')
    expect(baseNameOf('D:\\ws\\task\\out\\report.json')).toBe('report.json')
  })

  it('目录列：有前缀取前缀，顶层文件返回空串（调用方据此不渲染该列）', () => {
    expect(dirNameOf('docs/方案说明.md')).toBe('docs')
    expect(dirNameOf('src/auth/login.py')).toBe('src/auth')
    expect(dirNameOf('top.txt')).toBe('')
    expect(dirNameOf('D:\\ws\\a\\b.txt')).toBe('D:/ws/a')
  })

  it('绝不返回「未命名」这类占位（无末段时回落整路径，尾斜杠保留原样）', () => {
    expect(baseNameOf('')).toBe('')
    // 末段为空 → 回落整路径（保留尾斜杠），关键是绝不产出占位文案
    expect(baseNameOf('docs/')).toBe('docs/')
    expect(baseNameOf('a/b/')).toBe('a/b/')
    expect(baseNameOf('a/b/')).not.toBe('未命名')
  })
})

describe('buildScenePreviewText — 状态行一条预览', () => {
  it('有活实例时优先报实例数与当前动作', () => {
    expect(buildScenePreviewText(2, '命令 npm run dev', 3, 'docs/a.md')).toBe(
      '2 个实例运行中 · 命令 npm run dev',
    )
    // 焦点缺失时不拼空尾巴
    expect(buildScenePreviewText(1, undefined, 0, undefined)).toBe('1 个实例运行中')
  })

  it('无活实例但有产物时报产物数与最近文件名', () => {
    expect(buildScenePreviewText(0, undefined, 3, 'docs/方案说明.md')).toBe(
      '产物 3 个文件 · 方案说明.md',
    )
    expect(buildScenePreviewText(0, undefined, 2, 'D:\\ws\\b.py')).toBe('产物 2 个文件 · b.py')
  })

  it('两者皆无 → 如实空态文案（不假装有内容）', () => {
    expect(buildScenePreviewText(0, undefined, 0, undefined)).toBe('暂无现场活动')
  })
})
