/** @feature FP-0.2.四 声明驱动查看器路由 | @ci: frontend-test */
/**
 * FilePreview — Excel 渲染 stale-write 竞态回归（审查复验新增 MF 的行为锚）
 *
 * 竞态面：ExcelBody 的 async 解析链（fetch → import → parseExcelWorkbook）
 * 期间文件切换（content/url 变化 → effect cleanup 置 cancelled=true），旧文件
 * 解析晚于新文件完成时，无「await 之后复查 cancelled」守卫会把过期 workbook
 * 写进新文件状态——查看器静默显示错误文件的内容。
 *
 * 修复契约（commit c8bf2f43a9）：await 之后必须复查 cancelled。
 *
 * 时序控制：fetch 与 parseExcelWorkbook 均为受控 deferred（时序测试铁律
 * 「注入可控延迟」——竞态窗口只有在解析耗时可操纵时才能确定性复现）；
 * 断言全部打在用户可见表格内容上，非内部调用次数。
 *
 * 删值实验已做：临时移除 await 后守卫 → 用例①「旧内容覆盖」变红；
 * 恢复守卫 → 全绿（证明本文件真实锁定竞态窗口，非恒绿摆设）。
 */
import { act, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FilePreview } from '@/components/workspace/FilePreview'

/** 工作簿桩（与 ExcelWorkbook 渲染消费面同形：rows/totalRows/truncated） */
interface WorkbookStub {
  sheets: Array<{ name: string; rows: string[][]; totalRows: number; truncated: boolean }>
}

const { parseCalls, fetchCalls } = vi.hoisted(() => ({
  parseCalls: [] as Array<{ bytes: Uint8Array; resolve: (wb: never) => void }>,
  fetchCalls: [] as Array<{ url: string; resolve: (bytes: Uint8Array) => void }>,
}))

vi.mock('@/components/workspace/excelSheet', () => ({
  parseExcelWorkbook: (bytes: Uint8Array): Promise<unknown> =>
    new Promise((resolve) => {
      parseCalls.push({ bytes, resolve: resolve as (wb: never) => void })
    }),
}))

beforeEach(() => {
  parseCalls.length = 0
  fetchCalls.length = 0
  // fetch 受控 deferred：按调用序记录，resolve 时机由用例操纵
  vi.stubGlobal(
    'fetch',
    vi.fn((input: RequestInfo | URL) => {
      const call: { url: string; resolve: (bytes: Uint8Array) => void } = {
        url: String(input),
        resolve: () => {},
      }
      const promise = new Promise<Uint8Array>((resolve) => {
        call.resolve = resolve
      })
      fetchCalls.push(call)
      return promise.then((bytes) => ({
        ok: true,
        status: 200,
        arrayBuffer: async () =>
          bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
      }))
    }),
  )
})

afterEach(() => {
  vi.unstubAllGlobals()
})

function makeWorkbook(cellText: string): WorkbookStub {
  return { sheets: [{ name: '数据', rows: [[cellText]], totalRows: 1, truncated: false }] }
}

describe('FilePreview ExcelBody — stale-write 竞态回归（审查复验 MF）', () => {
  it('旧文件解析晚于新文件完成：新内容保持，旧内容不得覆盖（await 后守卫）', async () => {
    const { rerender } = render(
      <FilePreview filePath="old.xlsx" content="" url="/old.xlsx" containerTaskId="" />,
    )
    expect(fetchCalls.length).toBe(1)

    // 旧文件完成取流、解析挂起（竞态前置态：停在 parseExcelWorkbook 窗口）
    fetchCalls[0]!.resolve(new TextEncoder().encode('old-bytes'))
    await waitFor(() => expect(parseCalls.length).toBe(1))

    // 切换文件：cleanup 置旧 effect cancelled=true，新 effect 启动
    rerender(<FilePreview filePath="new.xlsx" content="" url="/new.xlsx" containerTaskId="" />)
    expect(fetchCalls.length).toBe(2)

    // 小文件（新）完成取流与解析，先渲染
    fetchCalls[1]!.resolve(new TextEncoder().encode('new-bytes'))
    await waitFor(() => expect(parseCalls.length).toBe(2))
    parseCalls[1]!.resolve(makeWorkbook('新文件内容') as never)
    expect(await screen.findByText('新文件内容')).toBeInTheDocument()

    // 大文件（旧）解析后完成 → 竞态复现点：无守卫时此处会把旧内容写回
    parseCalls[0]!.resolve(makeWorkbook('旧文件内容') as never)
    await act(async () => {})

    expect(screen.queryByText('旧文件内容')).toBeNull()
    expect(screen.getByText('新文件内容')).toBeInTheDocument()
  })

  it('切换发生在取流期间：旧文件解析不得再被调用（await 前守卫）', async () => {
    const { rerender } = render(
      <FilePreview filePath="old.xlsx" content="" url="/old.xlsx" containerTaskId="" />,
    )
    expect(fetchCalls.length).toBe(1)

    rerender(<FilePreview filePath="new.xlsx" content="" url="/new.xlsx" containerTaskId="" />)
    expect(fetchCalls.length).toBe(2)

    // 新文件完成取流与解析
    fetchCalls[1]!.resolve(new TextEncoder().encode('new-bytes'))
    await waitFor(() => expect(parseCalls.length).toBe(1))
    parseCalls[0]!.resolve(makeWorkbook('新文件内容') as never)
    expect(await screen.findByText('新文件内容')).toBeInTheDocument()

    // 旧文件取流晚到 → 旧 effect 已 cancelled，解析不得触发
    fetchCalls[0]!.resolve(new TextEncoder().encode('old-bytes'))
    await act(async () => {})

    expect(parseCalls.length).toBe(1)
    expect(screen.queryByText('旧文件内容')).toBeNull()
    expect(screen.getByText('新文件内容')).toBeInTheDocument()
  })
})
