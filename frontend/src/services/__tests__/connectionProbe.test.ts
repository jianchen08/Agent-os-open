/** @ci: frontend-test */
/**
 * services/connectionProbe — 地址校验/规整与明文远程判定纯函数分支。
 *
 * validateServerBaseUrl：空输入=清除（null）/ 无协议补 http:// / http(s) 原样 /
 * 非法 scheme（file/ftp/ws 等）拒绝（后端连接只走 HTTP(S)）。
 * isPlaintextRemoteUrl：http 且非回环 = true（P6 明文过渡期提示的判定面）；
 * https 恒 false；回环（127.x/localhost/::1）http 恒 false。
 */
import { describe, expect, it } from 'vitest'
import {
  isPlaintextRemoteUrl,
  validateServerBaseUrl,
} from '../connectionProbe'

describe('validateServerBaseUrl — 合法输入规整', () => {
  it('空输入 → 清除语义（url=null）', () => {
    expect(validateServerBaseUrl('')).toEqual({ ok: true, url: null })
    expect(validateServerBaseUrl('   ')).toEqual({ ok: true, url: null })
  })

  it('无协议 → 补 http://', () => {
    expect(validateServerBaseUrl('192.168.1.10:9300')).toEqual({
      ok: true,
      url: 'http://192.168.1.10:9300',
    })
  })

  it('去首尾空白与结尾斜杠', () => {
    expect(validateServerBaseUrl('  http://cloud.example.com/  ')).toEqual({
      ok: true,
      url: 'http://cloud.example.com',
    })
  })

  it.each(['http://192.168.1.10', 'https://agent.example.com:8443/path'])(
    'http/https 原样保留（%s）',
    (url) => {
      expect(validateServerBaseUrl(url)).toEqual({ ok: true, url })
    },
  )
})

describe('validateServerBaseUrl — 非法 scheme 拒绝', () => {
  it.each([
    ['file:///etc/passwd', 'file'],
    ['ftp://192.168.1.10', 'ftp'],
    ['ws://192.168.1.10:9300', 'ws'],
    ['javascript://alert(1)', 'javascript'],
  ])('%s → 拒绝并指出协议', (input, scheme) => {
    const result = validateServerBaseUrl(input)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.reason).toContain(scheme)
    }
  })
})

describe('isPlaintextRemoteUrl — P6 明文过渡期判定', () => {
  it.each([
    ['http://192.168.1.10:9300', true],
    ['http://agent.example.com', true],
    ['http://203.0.113.153', true],
  ])('远程 http = true（%s）', (url, expected) => {
    expect(isPlaintextRemoteUrl(url)).toBe(expected)
  })

  it.each([
    ['https://agent.example.com', 'https 恒 false'],
    ['http://127.0.0.1:9300', '回环 ipv4 false'],
    ['http://localhost:9300', '回环 localhost false'],
    ['http://[::1]:9300', '回环 ipv6 false'],
  ])('%s（%s）', (url) => {
    expect(isPlaintextRemoteUrl(url)).toBe(false)
  })

  it('非 URL 输入安全返回 false（不抛）', () => {
    expect(isPlaintextRemoteUrl('not a url')).toBe(false)
  })
})
