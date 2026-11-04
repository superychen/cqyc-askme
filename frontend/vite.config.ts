import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  // 静态资源和 Vue Router 使用同一基路径；生产 /admin/* 回退到管理端 index.html。
  base: '/admin/',
  plugins: [vue()],
  server: { port: 5173, strictPort: true, proxy: { '/api': { target: 'http://127.0.0.1:8000', changeOrigin: false } } },
})
