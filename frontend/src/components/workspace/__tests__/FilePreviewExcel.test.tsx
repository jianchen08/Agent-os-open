/** @feature FP-0.2.四 声明驱动查看器路由 | @ci: frontend-test */
/**
 * FilePreview — Excel 渲染分支测试（excel_preview）
 *
 * 取流链路：fetch 是外部网络依赖，stub 返回真 xlsx 字节（真包构造）；
 * 解析与表格渲染走真实现零 mock（防拟合——实现无法靠硬编码期望值刷绿）。
 *
 * 覆盖（状态机 loading → success / empty / error）：
 * - success：多 sheet 工作簿渲染首 sheet 表格 + sheet 切换（2 组有区分度输入：
 *   中文表头数字数据 / 英文表头文本数据）
 * - truncated：超限表截断提示可见 + 行数如实
 * - error：取流失败（HTTP 500）显式错误态，不白屏不静默
 * - error：content 兜底为损坏文本（file-content 二进制解码局限的真实形态）
 *   时显式失败——该用例同时是「工作区二进制取流局限」的行为证据
 * - 数据缺失：无 url 无 content → 显式提示
 * - 类型判定：大写扩展名 .XLSX 同样命中 excel 分支
 */
import * as XLSX from '@e965/xlsx'
import { fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { EXCEL_PREVIEW_MAX_ROWS, parseExcelWorkbook } from '@/components/workspace/excelSheet'
import { FilePreview } from '@/components/workspace/FilePreview'

/** 用真包构造 xlsx 字节（写入 → 读回全真链路） */
function buildXlsxBytes(sheets: Array<{ name: string; aoa: unknown[][] }>): Uint8Array {
  const wb = XLSX.utils.book_new()
  for (const { name, aoa } of sheets) {
    XLSX.utils.book_append_sheet(wb, XLSX.utils.aoa_to_sheet(aoa), name)
  }
  const out = XLSX.write(wb, { bookType: 'xlsx', type: 'array' }) as ArrayBuffer
  return new Uint8Array(out)
}

/** stub 全局 fetch：返回真字节（success）或指定状态码（error 面外部网络依赖）。
 * 用最小 Response 形态对象（jsdom/node 环境差异下比真 Response 稳） */
function stubFetchBytes(bytes: Uint8Array, status = 200): void {
  vi.stubGlobal(
    'fetch',
    vi.fn(async () => ({
      ok: status >= 200 && status < 300,
      status,
      arrayBuffer: async () =>
        bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength),
    })),
  )
}

beforeEach(() => {
  vi.stubGlobal('URL', {
    ...URL,
    createObjectURL: vi.fn(() => 'blob:mock'),
    revokeObjectURL: vi.fn(),
  })
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('FilePreview — Excel 渲染（success）', () => {
  it('多 sheet 工作簿：默认渲染首个 sheet 表格，点击 sheet 名切换内容', async () => {
    const bytes = buildXlsxBytes([
      { name: '成绩', aoa: [['姓名', '分数'], ['小明', '95']] },
      { name: '说明', aoa: [['备注'], ['第二张表内容']] },
    ])
    stubFetchBytes(bytes)
    render(<FilePreview filePath="data/report.xlsx" content="" containerTaskId="ct-1" />)

    // 首个 sheet 内容可见（异步解析后）
    expect(await screen.findByText('小明')).toBeInTheDocument()
    expect(screen.getByText('95')).toBeInTheDocument()

    // 切换到第二个 sheet：内容随之变化（性质断言：两 sheet 内容互斥可见）
    fireEvent.click(screen.getByRole('button', { name: '说明' }))
    expect(await screen.findByText('第二张表内容')).toBeInTheDocument()
    expect(screen.queryByText('小明')).toBeNull()
  })

  it('英文表头文本数据同样正确渲染（第二组有区分度输入）', async () => {
    const bytes = buildXlsxBytes([{ name: 'Plan', aoa: [['task', 'owner'], ['write tests', 'alice']] }])
    stubFetchBytes(bytes)
    render(<FilePreview filePath="out/plan.XLSX" content="" containerTaskId="ct-1" />)
    expect(await screen.findByText('write tests')).toBeInTheDocument()
    expect(screen.getByText('alice')).toBeInTheDocument()
  })

  it('截断提示：超限表显示「仅显示前 N 行」且渲染行数 = N', async () => {
    const bigAoa: unknown[][] = [['h']]
    for (let i = 1; i <= EXCEL_PREVIEW_MAX_ROWS + 5; i += 1) bigAoa.push([`row-${i}`])
    stubFetchBytes(buildXlsxBytes([{ name: 'big', aoa: bigAoa }]))
    render(<FilePreview filePath="d/big.xlsx" content="" containerTaskId="ct-1" />)

    expect(await screen.findByText(new RegExp(`仅显示前 ${EXCEL_PREVIEW_MAX_ROWS} 行`))).toBeInTheDocument()
    // 解析层截断行数一致（真链路性质断言；async——OLE2 codepage 装载契约）
    const parsed = await parseExcelWorkbook(buildXlsxBytes([{ name: 'big', aoa: bigAoa }]))
    expect(parsed.sheets[0]!.rows).toHaveLength(EXCEL_PREVIEW_MAX_ROWS)
    // 首行表头在、超限尾行不在
    expect(screen.getByText('h')).toBeInTheDocument()
    expect(screen.queryByText(`row-${EXCEL_PREVIEW_MAX_ROWS + 1}`)).toBeNull()
  })
})

describe('FilePreview — Excel 渲染（error / empty）', () => {
  it('取流失败（HTTP 500）显式错误态，不白屏', async () => {
    stubFetchBytes(new Uint8Array([1, 2, 3]), 500)
    render(<FilePreview filePath="d/bad.xlsx" content="" containerTaskId="ct-1" />)
    expect(await screen.findByText(/Excel 渲染失败/)).toBeInTheDocument()
    expect(screen.queryByTestId('file-preview-excel')).toBeNull()
  })

  it('content 兜底为损坏文本（file-content 二进制解码局限的真实形态）时显式失败', async () => {
    // 工作区 file-content 端点对二进制做 UTF-8 replace 解码——content 非真字节，
    // 解析必失败；分支必须显式报错而不是渲染空表假装成功
    render(
      <FilePreview filePath="d/ws.xlsx" content="\ufffd\ufffd corrupted" containerTaskId="" />,
    )
    expect(await screen.findByText(/Excel 渲染失败/)).toBeInTheDocument()
  })

  it('无 url 无 content：提示缺少文件数据', async () => {
    render(<FilePreview filePath="d/empty.xlsx" content="" containerTaskId="" />)
    expect(await screen.findByText('缺少文件数据，无法预览')).toBeInTheDocument()
  })
})
