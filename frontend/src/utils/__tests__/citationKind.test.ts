// @feature FP-0.2.评审引用交互 R5：输入框引用卡片化 | @ci: frontend-test
/**
 * citationKind kind 判定注册表测试（2026-11-05 R5）。
 *
 * 契约：文件路径按扩展名分 code/doc/image；无扩展名/未知扩展名落 artifact
 * （制品路径形态）；大小写不敏感；注册缝可登记新扩展名映射。
 */
import { afterEach, describe, expect, it } from 'vitest'
import {
  classifyCitationKind,
  registerCitationExtensionKind,
} from '@/utils/citationKind'

afterEach(() => {
  // 注册缝用例注入的映射随注册表存续——清理用独立 unknown-kind 注册+移除不便，
  // 注入用内置 kind 覆盖内置扩展名无效（后注册覆盖先注册），为防串扰统一回归点：
  // 用例内注入的扩展名全部指向 'artifact'（内置无 artifact 扩展名，恢复等价）
  registerCitationExtensionKind(['.xyz', 'xyz', 'weirdext'], 'artifact')
})

describe('classifyCitationKind（扩展名 → kind）', () => {
  it('代码扩展名 → code（含目录前缀与大小写不敏感，两组以上）', () => {
    expect(classifyCitationKind('src/main.py')).toBe('code')
    expect(classifyCitationKind('kernel/crates/core/lib.rs')).toBe('code')
    expect(classifyCitationKind('D:\\proj\\APP.TS')).toBe('code')
  })

  it('文档/图片扩展名各归其类（两组以上）', () => {
    expect(classifyCitationKind('docs/设计.md')).toBe('doc')
    expect(classifyCitationKind('/tmp/report.PDF')).toBe('doc')
    expect(classifyCitationKind('assets/logo.png')).toBe('image')
    expect(classifyCitationKind('shot.jpeg')).toBe('image')
  })

  it('无扩展名/未知扩展名/隐藏文件 → artifact（制品路径形态兜底）', () => {
    expect(classifyCitationKind('artifacts/run-123/snapshot')).toBe('artifact')
    expect(classifyCitationKind('scene.weirdext')).toBe('artifact')
    expect(classifyCitationKind('.gitignore')).toBe('artifact')
    expect(classifyCitationKind('noext')).toBe('artifact')
  })

  it('注册缝：新扩展名映射可注册生效', () => {
    registerCitationExtensionKind(['weirdext'], 'doc')
    expect(classifyCitationKind('scene.weirdext')).toBe('doc')
  })
})
