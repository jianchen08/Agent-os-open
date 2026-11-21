/**
 * 回放链接前端加密：WebCrypto AES-256-GCM + key-in-fragment。
 *
 * 模式参考 Yopass（Apache-2.0，docs/working/dsh-go移动端本地运行调研与接入工作量_20261112.md
 * §四B）：密钥由浏览器随机生成，只随 URL fragment（#k=...）传递——fragment
 * 不进 HTTP 请求，服务端只见密文不可解密。加密在上传前、解密在取回后，
 * 全部走本模块，服务端（plugins/shared/replay_link）只存 ciphertext_b64/iv_b64。
 */

/** 随机生成 256 位密钥并 base64url 编码（分享链接密钥面） */
export async function generateKeyB64Url(): Promise<string> {
  const raw = crypto.getRandomValues(new Uint8Array(32))
  return bytesToB64Url(raw)
}

/** 明文（任意 JSON 字符串）→ {ciphertext_b64, iv_b64}（AES-256-GCM，12 字节随机 IV） */
export async function encryptReplay(
  plaintext: string,
  keyB64Url: string,
): Promise<{ ciphertext_b64: string; iv_b64: string }> {
  const key = await importKey(keyB64Url)
  const iv = crypto.getRandomValues(new Uint8Array(12))
  const cipher = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv },
    key,
    new TextEncoder().encode(plaintext),
  )
  return {
    ciphertext_b64: bytesToB64Url(new Uint8Array(cipher)),
    iv_b64: bytesToB64Url(iv),
  }
}

/** {ciphertext_b64, iv_b64} + 密钥 → 明文；密钥错误/密文被篡改抛错（GCM 认证标签） */
export async function decryptReplay(
  ciphertextB64: string,
  ivB64: string,
  keyB64Url: string,
): Promise<string> {
  const key = await importKey(keyB64Url)
  const iv = b64UrlToBytes(ivB64)
  const plain = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv },
    key,
    b64UrlToBytes(ciphertextB64),
  )
  return new TextDecoder().decode(plain)
}

/** 组装分享链接：<origin><path>#k=<key>（密钥只进 fragment） */
export function buildShareLink(baseUrl: string, recordId: string, keyB64Url: string): string {
  const url = new URL(baseUrl)
  url.searchParams.set('id', recordId)
  url.hash = `k=${keyB64Url}`
  return url.toString()
}

/** 从当前 location（或任意链接串）解析 fragment 密钥；无 k= 返回 null */
export function extractKeyFromFragment(urlOrHash: string): string | null {
  const hash = urlOrHash.includes('#') ? urlOrHash.slice(urlOrHash.indexOf('#') + 1) : urlOrHash
  const params = new URLSearchParams(hash)
  const k = params.get('k')
  return k && k.length > 0 ? k : null
}

async function importKey(keyB64Url: string): Promise<CryptoKey> {
  const raw = b64UrlToBytes(keyB64Url)
  if (raw.length !== 32) {
    throw new Error(`invalid key length: ${raw.length} (expect 32)`)
  }
  return crypto.subtle.importKey('raw', raw, { name: 'AES-GCM' }, false, ['encrypt', 'decrypt'])
}

function bytesToB64Url(bytes: Uint8Array): string {
  let bin = ''
  bytes.forEach((b) => {
    bin += String.fromCharCode(b)
  })
  return btoa(bin).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

function b64UrlToBytes(s: string): Uint8Array<ArrayBuffer> {
  const b64 = s.replace(/-/g, '+').replace(/_/g, '/') + '='.repeat((4 - (s.length % 4)) % 4)
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return bytes
}
