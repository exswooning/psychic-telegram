// No config file existed at all -- `npm run lint` has never actually run
// since this project began (confirmed: no .eslintrc*/eslint.config.* in
// git history, --max-warnings 0 in package.json's own script never
// exercised). .cjs, not .js: package.json has "type": "module", and
// ESLint 8's config loader needs CommonJS `module.exports`.
//
// Matches the plugin set already in package.json's devDependencies
// (@typescript-eslint, react-hooks, react-refresh) -- the standard Vite
// React+TS template's config, which is what those exact versions imply
// was intended.
module.exports = {
  root: true,
  env: { browser: true, es2020: true },
  extends: [
    'eslint:recommended',
    'plugin:@typescript-eslint/recommended',
    'plugin:react-hooks/recommended',
  ],
  ignorePatterns: ['dist', '.eslintrc.cjs'],
  parser: '@typescript-eslint/parser',
  plugins: ['react-refresh'],
  rules: {
    'react-refresh/only-export-components': [
      'warn',
      { allowConstantExport: true },
    ],
    // Matches tsconfig.json's own noUnusedLocals/noUnusedParameters: false
    // -- the project already tolerates this at the type-checker level;
    // enforcing it only in the linter would be a new, unannounced rule.
    '@typescript-eslint/no-unused-vars': 'off',
  },
}
