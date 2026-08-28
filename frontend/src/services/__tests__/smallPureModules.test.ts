// @feature: FP-0.2.四 前端Schema | @ci: frontend-test
/**
 * 文件编辑器配置 / 布局解析器 / 认证回调注册 测试
 *
 * 纯逻辑小模块：getEditorForFile 扩展名映射、safeLoadLayout 合并、
 * registerAuthExpiredCallback / triggerAuthExpired 回调触发。
 */
import { describe, it, expect, vi } from 'vitest'
import { getEditorForFile } from '@/config/fileEditors'
import { safeLoadLayout, DEFAULT_LAYOUT_CONFIG } from '@/services/layout/resolver'
import { registerAuthExpiredCallback, triggerAuthExpired } from '@/services/authCallbacks'

describe('getEditorForFile - 扩展名到编辑器映射', () => {
  it('常见扩展名映射到文本编辑器/图片查看器/HTML 预览', () => {
    expect(getEditorForFile('main.py').id).toBe('text_editor')
    expect(getEditorForFile('README.md').id).toBe('text_editor')
    expect(getEditorForFile('index.html').id).toBe('html_preview')
    expect(getEditorForFile('photo.png').id).toBe('image_viewer')
    expect(getEditorForFile('photo.JPG').id).toBe('image_viewer') // 大小写不敏感
  })

  it('无扩展名文件（Makefile/.gitignore）→ 按小写全名匹配或回退默认', () => {
    expect(getEditorForFile('Makefile').id).toBe('text_editor')
    expect(getEditorForFile('.gitignore').id).toBe('text_editor')
  })

  it('未知扩展名 → 回退默认文本编辑器', () => {
    expect(getEditorForFile('archive.zzz').id).toBe('text_editor')
    expect(getEditorForFile('noext').id).toBe('text_editor')
  })

  it('带目录路径的文件名 → 只取 basename 匹配', () => {
    expect(getEditorForFile('src/deep/nested/main.tsx').id).toBe('text_editor')
    expect(getEditorForFile('C:\\work\\a\\b\\pic.webp').id).toBe('image_viewer')
  })
})

describe('safeLoadLayout - 布局配置安全合并', () => {
  it('undefined → 返回默认布局（同一引用）', () => {
    expect(safeLoadLayout(undefined)).toBe(DEFAULT_LAYOUT_CONFIG)
  })

  it('部分覆盖：只合并传入字段，其余字段取默认', () => {
    const merged = safeLoadLayout({ sidebar: { defaultWidth: 320 } } as any)
    expect(merged.sidebar.defaultWidth).toBe(320)
    expect(merged.sidebar.minWidth).toBe(240) // 默认保留
    expect(merged.chatPanel.defaultWidth).toBe(520) // 未提供字段取默认
    expect(merged.dockBar.height).toBe(22)
    expect(merged.zIndex.floatingWindow).toBe(50)
  })

  it('完整覆盖：所有组块都来自 themeLayout', () => {
    const theme = {
      breakpoints: { mobile: 100 },
      sidebar: { defaultWidth: 999 },
      chatPanel: { defaultWidth: 111 },
      workspacePanel: { defaultWidth: 222 },
      floatingWindow: { defaultWidth: 333, defaultHeight: 444 },
      dockBar: { height: 99 },
      panelSplit: { chatRatio: 0.7 },
      gaps: { spacePadding: 4 },
      transitions: { panelDuration: 500 },
      zIndex: { sidebar: 5 },
    }
    const merged = safeLoadLayout(theme as any)
    expect(merged.breakpoints.mobile).toBe(100)
    expect(merged.sidebar.defaultWidth).toBe(999)
    expect(merged.chatPanel.defaultWidth).toBe(111)
    expect(merged.workspacePanel.defaultWidth).toBe(222)
    expect(merged.floatingWindow.defaultWidth).toBe(333)
    expect(merged.dockBar.height).toBe(99)
    expect(merged.panelSplit.chatRatio).toBe(0.7)
    expect(merged.gaps.spacePadding).toBe(4)
    expect(merged.transitions.panelDuration).toBe(500)
    expect(merged.zIndex.sidebar).toBe(5)
  })
})

describe('authCallbacks - 认证过期回调注册/触发', () => {
  it('未注册回调时 trigger 不抛错', () => {
    expect(() => triggerAuthExpired()).not.toThrow()
  })

  it('注册回调后 trigger 触发该回调（注册-触发-再注册替换）', () => {
    const cb1 = vi.fn()
    const cb2 = vi.fn()
    registerAuthExpiredCallback(cb1)
    triggerAuthExpired()
    expect(cb1).toHaveBeenCalledTimes(1)

    registerAuthExpiredCallback(cb2) // 替换
    triggerAuthExpired()
    expect(cb1).toHaveBeenCalledTimes(1) // 旧回调不再触发
    expect(cb2).toHaveBeenCalledTimes(1)
  })
})
