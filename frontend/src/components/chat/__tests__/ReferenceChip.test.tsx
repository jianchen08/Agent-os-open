/** @feature FP-T12 前端适配 | @ci: frontend-test */
/**
 * 功能测试：通用引用卡片组件 ReferenceChip
 *
 * 覆盖：默认渲染（标题/副标题/kind 徽章/缩略图）、kind 渲染器注册扩展、
 * 引用消息内容解析 parseReferenceMessage。
 */

import { render, screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import {
  parseReferenceMessage,
  ReferenceChip,
  registerReferenceRenderer,
} from '@/components/chat/ReferenceChip'

describe('ReferenceChip 默认渲染', () => {
  it('渲染标题、副标题与 kind 徽章', () => {
    render(
      <ReferenceChip
        data={{ kind: 'godot-node', title: 'Player', subtitle: 'Sprite2D @ Node2D/Player' }}
      />,
    )

    expect(screen.getByText('Player')).toBeInTheDocument()
    expect(screen.getByText('Sprite2D @ Node2D/Player')).toBeInTheDocument()
    expect(screen.getByText('godot-node')).toBeInTheDocument()
  })

  it('有 previewUrl 时渲染缩略图', () => {
    render(
      <ReferenceChip
        data={{ kind: 'godot-node', title: 'Player', previewUrl: '/ext/pipeline_godot_context/preview?index=0' }}
      />,
    )

    const img = screen.getByRole('img', { name: 'Player' })
    expect(img).toHaveAttribute('src', '/ext/pipeline_godot_context/preview?index=0')
  })
})

describe('ReferenceChip kind 渲染器注册（扩展性）', () => {
  it('注册自定义 kind 渲染器后由其接管渲染', () => {
    registerReferenceRenderer('test-kind', (data) => <strong>{`自定义:${data.title}`}</strong>)

    render(<ReferenceChip data={{ kind: 'test-kind', title: '设计稿A' }} />)

    expect(screen.getByText('自定义:设计稿A')).toBeInTheDocument()
  })

  it('未注册的 kind 仍走默认渲染', () => {
    render(<ReferenceChip data={{ kind: 'other-kind', title: '普通引用' }} />)

    expect(screen.getByText('普通引用')).toBeInTheDocument()
    expect(screen.getByText('other-kind')).toBeInTheDocument()
  })
})

describe('parseReferenceMessage 引用消息解析', () => {
  it('解析插件注入的 <reference> 消息（场景 + 条目）', () => {
    const content = [
      '<reference source="godot" scene="res://demo_main.tscn">',
      '- Player (Sprite2D) @ Node2D/Player',
      '- Camera2D (Camera2D) @ Node2D/Camera2D',
      '</reference>',
    ].join('\n')

    const parsed = parseReferenceMessage(content)

    expect(parsed).not.toBeNull()
    expect(parsed!.scene).toBe('res://demo_main.tscn')
    expect(parsed!.items).toHaveLength(2)
    expect(parsed!.items[0]).toEqual({ name: 'Player', type: 'Sprite2D', path: 'Node2D/Player' })
  })

  it('普通消息内容返回 null', () => {
    expect(parseReferenceMessage('对这个加个碰撞体')).toBeNull()
    expect(parseReferenceMessage('')).toBeNull()
  })
})
