import { defineConfig, loadEnv } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '')
  const apiTarget = env.VITE_API_TARGET || 'http://127.0.0.1:8000'
  return {
    plugins: [vue()],
    server: { proxy: { '/api': apiTarget, '/health': apiTarget, '/metrics': apiTarget } },
  }
})
