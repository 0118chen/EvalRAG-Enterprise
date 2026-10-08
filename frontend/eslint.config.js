// Flat ESLint config for the Vue 3 + TypeScript client.
//
// The rule set is deliberately only three presets -- ESLint's recommended set,
// typescript-eslint's recommended set and eslint-plugin-vue's recommended flat
// preset -- because formatting is not a lint concern here: Prettier owns layout
// and `npm run format:check` enforces it. `eslint-config-prettier` therefore
// sits last to switch off the few stylistic rules the presets still carry, so
// the two tools can never disagree.
import js from '@eslint/js'
import prettier from 'eslint-config-prettier'
import pluginVue from 'eslint-plugin-vue'
import tseslint from 'typescript-eslint'

export default tseslint.config(
  {
    // Build output, installed packages and the shared npm cache are not source.
    ignores: ['dist/**', 'node_modules/**', '_npmcache/**'],
  },
  js.configs.recommended,
  tseslint.configs.recommended,
  pluginVue.configs['flat/recommended'],
  {
    files: ['**/*.vue'],
    languageOptions: {
      parserOptions: {
        // `<script lang="ts">` blocks are TypeScript, so hand them to
        // typescript-eslint's parser after vue-eslint-parser splits the file.
        parser: tseslint.parser,
      },
    },
  },
  {
    rules: {
      // App.vue is the single shell mounted by main.ts, not a reusable
      // multi-word component name.
      'vue/multi-word-component-names': 'off',
    },
  },
  prettier,
)
