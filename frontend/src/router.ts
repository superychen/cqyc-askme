import { createRouter, createWebHistory } from 'vue-router'
import { auth, restoreSession } from './api'
import Login from './views/Login.vue'
import Imports from './views/Imports.vue'

export const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/login', component: Login },
  { path: '/', component: Imports, meta: { requiresAuth: true } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
] })
router.beforeEach(async (to) => {
  if (!auth.ready) { try { await restoreSession() } catch { /* 登录页将展示真实的服务错误。 */ } }
  if (to.meta.requiresAuth && !auth.authenticated) return '/login'
  if (to.path === '/login' && auth.authenticated) return '/'
})
