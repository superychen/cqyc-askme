import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 公开前台 5174、管理端 5173 共用8000后端；生产由 Nginx 按 /admin/、/api/ 分流。
export default defineConfig({plugins: [vue()], server: {port: 5174, strictPort: true, proxy: {
  '/api': {target: 'http://127.0.0.1:8000', changeOrigin: false},
  '/admin': {target: 'http://127.0.0.1:5173', changeOrigin: false},
}}})
