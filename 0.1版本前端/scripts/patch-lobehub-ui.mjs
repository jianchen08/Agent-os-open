import { readFileSync, writeFileSync } from 'fs';
import { join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const root = join(__dirname, '..');

const patches = [
  {
    file: 'node_modules/@lobehub/ui/es/Image/components/usePreview.mjs',
    from: 'rootClassName: cx(styles.preview, rootClassName),',
    to: 'classNames: { root: cx(styles.preview, rootClassName) },',
  },
  {
    file: 'node_modules/@lobehub/ui/es/Image/components/usePreviewGroup.mjs',
    from: 'rootClassName: cx(styles.preview, rootClassName),',
    to: 'classNames: { root: cx(styles.preview, rootClassName) },',
  },
];

let patched = 0;
for (const patch of patches) {
  const filePath = join(root, patch.file);
  try {
    let content = readFileSync(filePath, 'utf-8');
    if (content.includes(patch.from)) {
      content = content.replace(patch.from, patch.to);
      writeFileSync(filePath, content, 'utf-8');
      console.log(`[patch] Fixed: ${patch.file}`);
      patched++;
    } else if (content.includes(patch.to)) {
      console.log(`[patch] Already applied: ${patch.file}`);
    } else {
      console.warn(`[patch] Pattern not found: ${patch.file}`);
    }
  } catch {
    console.warn(`[patch] File not found: ${patch.file}`);
  }
}
console.log(`[patch] ${patched} file(s) patched`);
