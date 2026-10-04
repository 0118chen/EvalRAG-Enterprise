import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

// Vitest gets its own config instead of reusing vite.config.ts so the `test`
// block stays typed (vite's own defineConfig rejects it) and so `npm run build`
// keeps using the plain Vite config.
export default defineConfig({
  plugins: [vue()],
  test: {
    environment: 'jsdom',
    include: ['src/**/*.test.ts'],
    // Worker threads, not forked processes: the point of these tests is that
    // they run everywhere the build runs, including sandboxes that refuse to
    // spawn child processes with piped stdio.
    pool: 'threads',
  },
})
