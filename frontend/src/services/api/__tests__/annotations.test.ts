/** @feature FP-0.2.四 前端Schema（文档批注） @vision V6 可即用 @ci frontend-test */
/**
 * 批注 API 封装测试（任务现场批 5）。
 *
 * apiClient 是外部依赖（网络层），用可控 mock 替换并断言请求契约；
 * 返回值映射断言可观察输出：
 * - list 两种响应形态（裸数组 / {items}）与兜底空数组；
 * - create 的 snake_case 请求体（author_id 条件携带）；
 * - update/delete/resolve 的路径模板替换与请求体；
 * - annotationsToMarkdown（表头/条目/已解决态/目标摘要/无选项）与 describeTarget
 *   （文本选区截断、无文本只有区间、整篇、未知类型）。
 */
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  annotationsToMarkdown,
  createAnnotation,
  deleteAnnotation,
  describeTarget,
  listAnnotations,
  resolveAnnotation,
  updateAnnotation,
} from '../annotations'
import type { Annotation } from '@/types/artifact'

// vi.hoisted：mock 句柄提升到 vi.mock 工厂执行点可用（导入全置顶，消 import 组序违规）
const { getMock, postMock, putMock, deleteMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  putMock: vi.fn(),
  deleteMock: vi.fn(),
}))

vi.mock('../client', () => {
  const mockClient = {
    get: (...args: unknown[]) => getMock(...args),
    post: (...args: unknown[]) => postMock(...args),
    put: (...args: unknown[]) => putMock(...args),
    delete: (...args: unknown[]) => deleteMock(...args),
  }
  return { apiClient: mockClient }
})

function makeAnnotation(overrides: Partial<Annotation> = {}): Annotation {
  return {
    id: 'ann-1',
    artifactId: 'art-1',
    targetType: 'text_selection',
    targetData: { startOffset: 3, endOffset: 9, selectedText: '选中片段' },
    content: '批注内容',
    authorType: 'user',
    authorId: 'u1',
    status: 'open',
    createdAt: '2026-11-17T00:00:00.000Z',
    ...overrides,
  }
}

afterEach(() => {
  getMock.mockReset()
  postMock.mockReset()
  putMock.mockReset()
  deleteMock.mockReset()
})

describe('annotations API — 请求契约', () => {
  it('listAnnotations：裸数组响应原样返回，路径带 artifact_id', async () => {
    const items = [makeAnnotation()]
    getMock.mockResolvedValue({ data: items })
    await expect(listAnnotations('art-42')).resolves.toEqual(items)
    expect(getMock).toHaveBeenCalledWith('/ext/artifacts/art-42/annotations')
  })

  it('listAnnotations：{items} 包装形态解包；空对象兜底空数组', async () => {
    getMock.mockResolvedValue({ data: { items: [makeAnnotation()] } })
    await expect(listAnnotations('a')).resolves.toHaveLength(1)
    getMock.mockResolvedValue({ data: { nope: 1 } })
    await expect(listAnnotations('a')).resolves.toEqual([])
  })

  it('createAnnotation：snake_case 请求体，author_id 缺省不携带', async () => {
    const created = makeAnnotation({ id: 'ann-2' })
    postMock.mockResolvedValue({ data: created })
    await expect(
      createAnnotation('art-9', {
        targetType: 'text_selection',
        targetData: { startOffset: 0, endOffset: 4, selectedText: 'abcd' },
        content: 'hello',
      }),
    ).resolves.toEqual(created)
    expect(postMock).toHaveBeenCalledWith('/ext/artifacts/art-9/annotations', {
      target_type: 'text_selection',
      target_data: { startOffset: 0, endOffset: 4, selectedText: 'abcd' },
      content: 'hello',
      author_type: 'user',
    })
  })

  it('createAnnotation：显式 authorType/authorId 全量进请求体', async () => {
    postMock.mockResolvedValue({ data: makeAnnotation() })
    await createAnnotation('a', {
      targetType: 'whole_artifact',
      targetData: {},
      content: 'c',
      authorType: 'agent',
      authorId: 'agent-7',
    })
    expect(postMock).toHaveBeenCalledWith(expect.any(String), {
      target_type: 'whole_artifact',
      target_data: {},
      content: 'c',
      author_type: 'agent',
      author_id: 'agent-7',
    })
  })

  it('update/delete/resolve：路径模板替换 + 请求体形态', async () => {
    putMock.mockResolvedValue({ data: makeAnnotation() })
    await updateAnnotation('ann-9', 'new text')
    expect(putMock).toHaveBeenCalledWith('/ext/artifacts/annotations/ann-9', { content: 'new text' })

    deleteMock.mockResolvedValue({ data: undefined })
    await deleteAnnotation('ann-9')
    expect(deleteMock).toHaveBeenCalledWith('/ext/artifacts/annotations/ann-9')

    postMock.mockResolvedValue({ data: makeAnnotation({ status: 'resolved' }) })
    await resolveAnnotation('ann-9', false)
    expect(postMock).toHaveBeenCalledWith('/ext/artifacts/annotations/ann-9/resolve', {
      resolved: false,
    })
  })
})

describe('annotationsToMarkdown — 导出纯函数', () => {
  it('表头含文档名/产物 ID/数量/存储说明；条目含作者与目标摘要', () => {
    const md = annotationsToMarkdown([makeAnnotation()], {
      documentName: 'spec.docx',
      artifactId: 'art-1',
    })
    expect(md).toContain('# 批注导出 · spec.docx')
    expect(md).toContain('- 产物 ID: art-1')
    expect(md).toContain('- 批注数量: 1')
    expect(md).toContain('会话内内存态')
    expect(md).toContain('## 1. 用户')
    expect(md).toContain('> [3-9] 「选中片段」')
    expect(md).toContain('批注内容')
  })

  it('无选项 + 已解决 + 机器作者 + 长文本截断', () => {
    const long = 'x'.repeat(50)
    const md = annotationsToMarkdown(
      [makeAnnotation({ status: 'resolved', authorType: 'agent', targetData: { startOffset: 1, endOffset: 2, selectedText: long } })],
    )
    expect(md).toContain('## 1. 机器（已解决）')
    expect(md).toContain(`「${'x'.repeat(40)}…」`)
    expect(md).not.toContain('产物 ID')
  })

  it('空列表导出只有表头', () => {
    const md = annotationsToMarkdown([])
    expect(md).toContain('- 批注数量: 0')
    expect(md).not.toContain('## 1.')
  })
})

describe('describeTarget — 目标摘要', () => {
  it('整篇文档；未知类型空串；只有区间无文本', () => {
    expect(describeTarget(makeAnnotation({ targetType: 'whole_artifact', targetData: {} }))).toBe(
      '整篇文档',
    )
    expect(
      describeTarget(makeAnnotation({ targetType: 'other' as Annotation['targetType'], targetData: {} })),
    ).toBe('')
    expect(
      describeTarget(
        makeAnnotation({ targetData: { startOffset: 2, endOffset: 5, selectedText: '' } }),
      ),
    ).toBe('[2-5]')
  })
})
