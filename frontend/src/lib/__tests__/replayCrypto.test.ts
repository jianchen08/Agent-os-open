/** @feature FP-0.2.〇 核心批·回放链接 | @vision V6 可即用
 * 回放链接加密单测：密钥生成 → 加密 → 解密往返 + 篡改/错钥必败 + fragment 组装/解析。
 * ≥2 组区分度输入 + 性质断言（防拟合）。
 */
// @vitest-environment jsdom
import { describe, expect, it } from 'vitest'
import {
  buildShareLink,
  decryptReplay,
  encryptReplay,
  extractKeyFromFragment,
  generateKeyB64Url,
} from '../replayCrypto'

describe('回放链接加密往返（AES-256-GCM）', () => {
  it('性质：加密→解密往返恢复明文（多组区分度输入）', async () => {
    const key = await generateKeyB64Url()
    for (const plaintext of ['{"v":1,"msgs":[]}', '中文回放内容 🎉', 'a']) {
      const { ciphertext_b64, iv_b64 } = await encryptReplay(plaintext, key)
      // 泄漏断言只对 ≥8 字节明文生效：1 字节明文（如 'a'）的 base64 密文天然
      // 可能含该字符（~30% 概率假红，功能验证报告 §六）——短明文只验往返。
      if (plaintext.length >= 8) {
        expect(ciphertext_b64).not.toContain(plaintext)
      }
      const recovered = await decryptReplay(ciphertext_b64, iv_b64, key)
      expect(recovered).toBe(plaintext)
    }
  })

  it('同一明文两次加密密文不同（随机 IV 性质）', async () => {
    const key = await generateKeyB64Url()
    const a = await encryptReplay('same text', key)
    const b = await encryptReplay('same text', key)
    expect(a.ciphertext_b64).not.toBe(b.ciphertext_b64)
    expect(a.iv_b64).not.toBe(b.iv_b64)
  })

  it('错钥解密必败（GCM 认证标签）', async () => {
    const keyA = await generateKeyB64Url()
    const keyB = await generateKeyB64Url()
    expect(keyA).not.toBe(keyB)
    const { ciphertext_b64, iv_b64 } = await encryptReplay('secret', keyA)
    await expect(decryptReplay(ciphertext_b64, iv_b64, keyB)).rejects.toThrow()
  })

  it('密文被篡改解密必败', async () => {
    const key = await generateKeyB64Url()
    const { ciphertext_b64, iv_b64 } = await encryptReplay('secret', key)
    const bytes = ciphertext_b64.split('')
    bytes[0] = bytes[0] === 'A' ? 'B' : 'A'
    await expect(decryptReplay(bytes.join(''), iv_b64, key)).rejects.toThrow()
  })

  it('密钥长度非 32 字节拒绝导入', async () => {
    await expect(encryptReplay('x', 'AAAA')).rejects.toThrow('invalid key length')
  })
})

describe('key-in-fragment 链接组装与解析', () => {
  it('buildShareLink：密钥只进 fragment、id 进 query', () => {
    const link = buildShareLink('https://h.example.com/replay', 'rec123', 'KEY-abc')
    expect(link).toContain('#k=KEY-abc')
    expect(link).toContain('id=rec123')
    // 密钥不进 query（不随 HTTP 请求出）
    expect(link).not.toMatch(/[?&]k=/)
  })

  it('extractKeyFromFragment：从链接/纯 hash 解出密钥；无 k 返回 null', () => {
    expect(extractKeyFromFragment('https://h.example.com/r?id=1#k=ABC')).toBe('ABC')
    expect(extractKeyFromFragment('#k=XYZ')).toBe('XYZ')
    expect(extractKeyFromFragment('https://h.example.com/r?id=1')).toBeNull()
    expect(extractKeyFromFragment('#')).toBeNull()
  })
})
