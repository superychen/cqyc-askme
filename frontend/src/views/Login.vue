<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ArrowRight, Connection, Lock, User, Document, Picture, DataAnalysis } from '@element-plus/icons-vue'
import { auth, request } from '../api'

const router = useRouter()
const form = reactive({ username: '', password: '' })
const busy = ref(false), error = ref('')
async function submit() {
  if (busy.value) return
  if (!form.username.trim() || !form.password) { error.value = '请输入管理员账号和密码'; return }
  busy.value = true; error.value = ''
  try {
    const session = await request('/auth/login', 'POST', form)
    auth.csrf = session.csrf_token; auth.authenticated = true; auth.ready = true; auth.uploadLimit = session.upload_max_bytes
    form.password = ''; await router.replace('/')
  } catch (cause) { error.value = cause instanceof Error ? cause.message : '登录失败' }
  finally { busy.value = false }
}
</script>

<template>
  <main class="login-page">
    <div class="login-orb orb-one"></div><div class="login-orb orb-two"></div>
    <header class="login-header"><a class="brand" href="/login"><span class="brand-mark"><el-icon><Connection /></el-icon></span><strong>AskMe<span class="brand-dot">.</span></strong></a><span class="subtle">个人知识库管理平台</span></header>
    <div class="login-layout">
      <section class="login-story">
        <span class="eyebrow"><i></i> PERSONAL KNOWLEDGE STUDIO</span>
        <h1>把你的经历，<br>变成<span class="gradient-text">可探索的知识。</span></h1>
        <p>从一份文档开始，连接经验与表达。<br>整理你的故事，让每一次了解都有据可循。</p>
        <div class="knowledge-scene" aria-hidden="true">
          <div class="orbit-ring ring-a"></div><div class="orbit-ring ring-b"></div>
          <div class="scene-core"><el-icon><Connection /></el-icon><span>YOUR KNOWLEDGE</span></div>
          <div class="scene-card card-doc"><el-icon><Document /></el-icon><span>文档资料</span><i></i></div>
          <div class="scene-card card-img"><el-icon><Picture /></el-icon><span>图像理解</span><i></i></div>
          <div class="scene-card card-vector"><el-icon><DataAnalysis /></el-icon><span>语义连接</span><i></i></div>
        </div>
      </section>
      <section class="login-panel">
        <div class="panel-topline"></div><div class="login-icon"><el-icon><Lock /></el-icon></div>
        <span class="eyebrow">ADMIN ACCESS</span><h2>欢迎回到知识工作台</h2><p class="subtle">登录管理员账号，开始整理你的知识。</p>
        <form @submit.prevent="submit" class="login-form">
          <label for="username">管理员账号</label><el-input id="username" v-model="form.username" :prefix-icon="User" placeholder="输入管理员账号" autocomplete="username" size="large" maxlength="128" />
          <label for="password">登录密码</label><el-input id="password" v-model="form.password" :prefix-icon="Lock" type="password" show-password placeholder="输入登录密码" autocomplete="current-password" size="large" maxlength="512" />
          <el-alert v-if="error" :title="error" type="error" :closable="false" show-icon role="alert" />
          <el-button class="login-submit" type="primary" native-type="submit" :loading="busy" size="large">进入工作台<el-icon><ArrowRight /></el-icon></el-button>
        </form>
        <div class="login-note"><el-icon><Lock /></el-icon><span>仅限管理员访问 · 凭据由服务端环境配置管理</span></div>
      </section>
    </div>
    <footer class="login-footer"><span>AskMe · 为你的知识建立连接</span><span>KNOWLEDGE, CONNECTED.</span></footer>
  </main>
</template>
