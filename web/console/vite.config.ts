import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Web Console 前端。
// · dev: 5173，/api 代理到本机的 NPCSidekick Runtime(8765)
// · build: 产物 dist/ 由 npc/server.py 挂在 /console/ 下 —— 所以 base 必须是相对路径
export default defineConfig({
  base: './',
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8765', changeOrigin: false },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
