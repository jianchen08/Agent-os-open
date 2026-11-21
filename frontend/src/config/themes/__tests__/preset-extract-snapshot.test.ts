/** @feature FP-0.2.四 前端主题 | @ci: frontend-test */
/**
 * 预设主题提取快照锚（theme-contrast 门禁双实现一致性锁，2026-11-07）。
 *
 * theme-contrast 门禁（scripts/check_theme_contrast.py，Python 侧）静态解析
 * presets/*.ts 的对象字面量做 WCAG 对比度核算；本测试用 presetThemes 真实现
 * 锁定同一份快照（presets-extract-snapshot.json，由
 * `python scripts/check_theme_contrast.py` 的提取器生成、pytest 侧同步锚定）：
 * 预设数据变更未再生快照 / 任一侧提取或数学漂移 → 本测试或 pytest 侧红。
 * 快照再生：python -c "import importlib.util,sys,json,pathlib;
 * spec=importlib.util.spec_from_file_location('c','scripts/check_theme_contrast.py');
 * m=importlib.util.module_from_spec(spec);sys.modules['c']=m;spec.loader.exec_module(m);
 * p=m.load_preset_themes();pathlib.Path(m.SNAPSHOT_PATH).write_text(
 * json.dumps(p,ensure_ascii=False,indent=1,sort_keys=True)+'\n',encoding='utf-8')"
 */

import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { describe, expect, it } from 'vitest'
import { presetThemes } from '@/config/themes'

const SNAPSHOT_PATH = resolve(process.cwd(), 'src/config/themes/__tests__/presets-extract-snapshot.json')

describe('预设主题提取快照锚（theme-contrast 门禁一致性）', () => {
  const snapshot = JSON.parse(readFileSync(SNAPSHOT_PATH, 'utf-8')) as Record<string, unknown>
  const liveIds = Object.keys(presetThemes).sort()
  const snapshotIds = Object.keys(snapshot).sort()

  it('快照主题集合 ≡ presetThemes 真实现（双侧同步再生）', () => {
    expect(snapshotIds).toEqual(liveIds)
  })

  for (const id of liveIds) {
    it(`${id}: 提取结果逐字段一致`, () => {
      // JSON 往返归一（undefined 字段与快照缺键等价），只比数据面
      const live = JSON.parse(JSON.stringify(presetThemes[id]))
      expect(live).toEqual(snapshot[id])
    })
  }

  it('快照文件在位且非空（提取面漂移防护）', () => {
    expect(existsSync(SNAPSHOT_PATH)).toBe(true)
    expect(snapshotIds.length).toBeGreaterThan(0)
  })
})
