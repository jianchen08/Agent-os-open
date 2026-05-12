// 主题系统验证脚本
const fs = require('fs');
const path = require('path');

console.log('=== 主题系统完整性检查 ===\n');

// 1. 检查必需文件
const requiredFiles = [
  'src/config/themes/index.ts',
  'src/config/themes/presets/dark.ts',
  'src/config/themes/presets/light.ts',
  'src/services/themeService.ts',
  'src/services/themeStorage.ts',
  'src/stores/themeStore.ts',
  'src/types/theme.ts',
];

console.log('1. 检查必需文件:');
let allFilesExist = true;
requiredFiles.forEach(file => {
  const fullPath = path.join(process.cwd(), file);
  const exists = fs.existsSync(fullPath);
  console.log(`  ${exists ? '✅' : '❌'} ${file}`);
  if (!exists) allFilesExist = false;
});

// 2. 检查文件内容
console.log('\n2. 检查文件内容:');
const checks = [];

// 检查 dark.ts 导出
const darkTs = fs.readFileSync('src/config/themes/presets/dark.ts', 'utf-8');
checks.push({
  name: 'dark.ts 导出 darkTheme',
  pass: darkTs.includes('export const darkTheme')
});

// 检查 light.ts 导出
const lightTs = fs.readFileSync('src/config/themes/presets/light.ts', 'utf-8');
checks.push({
  name: 'light.ts 导出 lightTheme',
  pass: lightTs.includes('export const lightTheme')
});

// 检查 index.ts 重新导出
const indexTs = fs.readFileSync('src/config/themes/index.ts', 'utf-8');
checks.push({
  name: 'index.ts 重新导出 darkTheme',
  pass: indexTs.includes('export { darkTheme }')
});
checks.push({
  name: 'index.ts 重新导出 lightTheme',
  pass: indexTs.includes('export { lightTheme }')
});
checks.push({
  name: 'index.ts 导出 themeList',
  pass: indexTs.includes('export const themeList')
});

// 检查 themeStore.ts
const storeTs = fs.readFileSync('src/stores/themeStore.ts', 'utf-8');
checks.push({
  name: 'themeStore 导入 themeList',
  pass: storeTs.includes("import { themeList }")
});
checks.push({
  name: 'themeStore 导入 getPresetTheme',
  pass: storeTs.includes('getPresetTheme')
});
checks.push({
  name: 'themeStore 有 refreshThemes 方法',
  pass: storeTs.includes('refreshThemes:')
});

// 检查 themeService.ts
const serviceTs = fs.readFileSync('src/services/themeService.ts', 'utf-8');
checks.push({
  name: 'themeService 导入 darkTheme 和 lightTheme',
  pass: serviceTs.includes('darkTheme') && serviceTs.includes('lightTheme')
});

checks.forEach(check => {
  console.log(`  ${check.pass ? '✅' : '❌'} ${check.name}`);
});

// 3. 总结
console.log('\n3. 验证总结:');
const totalChecks = checks.length;
const passedChecks = checks.filter(c => c.pass).length;
console.log(`   文件检查: ${allFilesExist ? '✅ 通过' : '❌ 失败'}`);
console.log(`   内容检查: ${passedChecks}/${totalChecks} 通过`);
console.log(`   整体状态: ${allFilesExist && passedChecks === totalChecks ? '✅ 完整' : '❌ 不完整'}`);

process.exit(allFilesExist && passedChecks === totalChecks ? 0 : 1);
