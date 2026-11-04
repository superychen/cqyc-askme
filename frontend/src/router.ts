import { createRouter, createWebHistory } from 'vue-router'
import { auth, restoreSession } from './api'
import Login from './views/Login.vue'
import Imports from './views/Imports.vue'

// 管理端部署于 /admin/；内部 '/' 仍表示导入页，避免将后台登录守卫带入公开前台。
// 演示：router.push('/login') 生成 /admin/login，而不是覆盖公开站点根路径。
export const router = createRouter({ history: createWebHistory(import.meta.env.BASE_URL), routes: [
  { path: '/login', component: Login },
  { path: '/', component: Imports, meta: { requiresAuth: true } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
] })
router.beforeEach(async (to) => {
  if (!auth.ready) { try { await restoreSession() } catch { /* 登录页将展示真实的服务错误。 */ } }
  if (to.meta.requiresAuth && !auth.authenticated) return '/login'
  if (to.path === '/login' && auth.authenticated) return '/'
})
