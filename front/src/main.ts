import { createApp } from 'vue'
import App from './App.vue'
import './style.css'

// 无登录守卫：根入口直接挂载聊天页面，管理员认证由独立 frontend 工程负责。
createApp(App).mount('#app')
