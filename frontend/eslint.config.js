import js from '@eslint/js'
import globals from 'globals'
import tseslint from 'typescript-eslint'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import importX from 'eslint-plugin-import-x'

export default tseslint.config(
  // Global ignores
  {
    ignores: ['dist', 'node_modules', '*.config.js', '*.config.ts', '*.cjs'],
  },

  // Base JS + TS recommended rules
  js.configs.recommended,
  ...tseslint.configs.recommended,

  // Source files (non-test) - use tsconfig.app.json
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
      'import-x': importX,
    },
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.browser,
        ...globals.es2022,
      },
      parserOptions: {
        project: './tsconfig.eslint.json',
        tsconfigRootDir: import.meta.dirname,
      },
    },
    settings: {
      'import-x/resolver': {
        typescript: {
          alwaysTryTypes: true,
          project: './tsconfig.eslint.json',
        },
        node: true,
      },
    },
    rules: {
      // React Hooks - recommended rules (excludes extra-strict compiler rules
      // that produce false positives with React 19)
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'warn',

      // React Refresh - only for component files
      'react-refresh/only-export-components': [
        'warn',
        { allowConstantExport: true },
      ],

      // Import ordering: React -> third-party -> internal (@/) -> relative
      'import-x/order': [
        'warn',
        {
          groups: [
            'builtin',
            'external',
            'internal',
            ['parent', 'sibling', 'index'],
            'type',
          ],
          'newlines-between': 'never',
          alphabetize: { order: 'asc', caseInsensitive: true },
          distinctGroup: false,
        },
      ],
      'import-x/no-duplicates': 'warn',
      'import-x/no-unresolved': 'off',

      // TypeScript rules
      '@typescript-eslint/no-explicit-any': 'warn',
      '@typescript-eslint/no-unused-vars': [
        'warn',
        {
          argsIgnorePattern: '^_',
          varsIgnorePattern: '^_',
          caughtErrorsIgnorePattern: '^_',
        },
      ],
      '@typescript-eslint/consistent-type-imports': [
        'warn',
        { prefer: 'type-imports', fixStyle: 'inline-type-imports' },
      ],
      '@typescript-eslint/no-empty-interface': 'off',
      '@typescript-eslint/no-empty-object-type': 'off',
      '@typescript-eslint/no-non-null-assertion': 'warn',

      // General code quality
      'no-console': ['warn', { allow: ['warn', 'error'] }],
      'no-restricted-syntax': [
        'warn',
        {
          selector: 'JSXAttribute[name.name="style"] > JSXExpressionContainer',
          message:
            'Avoid inline styles. Use Tailwind CSS classes or a CSS module instead. If necessary, add an eslint-disable comment.',
        },
        // 统一审查 §一 / §3.3 P3：getStatusStyle 重复定义，统一到 shared/StatusBadge
        {
          selector: "FunctionDeclaration[id.name='getStatusStyle']",
          message:
            '禁止定义 getStatusStyle：使用 shared/StatusBadge（前端设计统一审查 §一）。',
        },
        {
          selector: "VariableDeclarator[id.name='getStatusStyle']",
          message:
            '禁止定义 getStatusStyle：使用 shared/StatusBadge（前端设计统一审查 §一）。',
        },
        // 统一审查 §二 N1：<a href="/..."> 导致整页刷新，统一到 useNavigate / PageShell
        {
          selector:
            "JSXOpeningElement[name.name='a'] > JSXAttribute[name.name='href'] > Literal[value=/^\\//]",
          message:
            '禁止 <a href="/..."> 做 SPA 内部导航（整页刷新）：使用 useNavigate 或 shared/PageShell（前端设计统一审查 N1）。',
        },
      ],

      // Relax overly strict defaults for React projects
      '@typescript-eslint/no-require-imports': 'off',
    },
  },

  // Test files - relaxed rules
  {
    files: [
      'src/**/*.test.{ts,tsx}',
      'src/**/__tests__/**/*.{ts,tsx}',
      'src/test/**/*.{ts,tsx}',
    ],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
      '@typescript-eslint/no-non-null-assertion': 'off',
      'no-console': 'off',
      'react-refresh/only-export-components': 'off',
    },
  },

  // Disable type-checked rules for JS files (config files, scripts, etc.)
  // 用 **/* 匹配嵌套目录（scripts/*.mjs 等）；原先 * 只匹配根目录，scripts 下 mjs 漏配。
  {
    files: ['**/*.{js,mjs,cjs}'],
    ...tseslint.configs.disableTypeChecked,
    languageOptions: {
      globals: {
        ...globals.node,
      },
    },
  },
)
