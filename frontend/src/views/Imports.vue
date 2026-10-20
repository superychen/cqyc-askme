<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { Connection, UploadFilled, Document, ArrowRight, Refresh, SwitchButton, CircleCheckFilled, Loading, WarningFilled, Clock, View, Files, Picture, DataAnalysis, FolderOpened } from '@element-plus/icons-vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { ApiError, auth, markdown, progressStream, request, uploadFile, type Task } from '../api'

const router = useRouter()
const tasks = ref<Task[]>([]), total = ref(0), page = ref(1), selected = ref<Task | null>(null)
const file = ref<File | null>(null), fileInput = ref<HTMLInputElement | null>(null)
const uploading = ref(false), uploadProgress = ref(0), dragging = ref(false), refreshing = ref(false)
const serviceError = ref(''), streamState = ref(''), retrying = ref(false)
const previewVisible = ref(false), previewBusy = ref(false), previewSource = ref(''), variant = ref('enriched')
const eventLog = ref<{ time: string; message: string }[]>([])
let stream: EventSource | null = null, poll: ReturnType<typeof setInterval> | null = null
const names: Record<string, string> = { queued: '排队中', running: '处理中', succeeded: '已完成', failed: '失败', interrupted: '已中断', skipped: '已跳过', pending: '待执行' }
const activeStep = computed(() => selected.value?.steps.find(step => step.id === selected.value?.node))
const applicable = computed(() => selected.value?.steps.filter(step => step.status !== 'skipped') || [])
const completed = computed(() => applicable.value.filter(step => step.status === 'succeeded').length)
const recentSucceeded = computed(() => tasks.value.filter(task => task.status === 'succeeded').length)
const recentChunks = computed(() => tasks.value.filter(task => task.status === 'succeeded').reduce((sum, task) => sum + task.chunk_count, 0))
const renderer = new MarkdownIt({ html: true, linkify: false })
DOMPurify.addHook('uponSanitizeAttribute', (node, data) => {
  // 不请求用户 MD 中任意远程图片；仅显示经管理员鉴权的 MinIO 代理图片。
  if (node.nodeName === 'IMG' && data.attrName === 'src' && !/^\/api\/v1\/admin\/assets\/[a-f0-9]{32}-[a-f0-9]{64}$/.test(data.attrValue)) data.keepAttr = false
})
const previewHtml = computed(() => DOMPurify.sanitize(renderer.render(previewSource.value), { FORBID_TAGS: ['iframe', 'style', 'script', 'form'], FORBID_ATTR: ['srcset', 'style'] }))

function size(bytes: number) { return bytes > 1024 * 1024 ? `${(bytes / 1024 / 1024).toFixed(1)} MB` : `${Math.max(1, Math.round(bytes / 1024))} KB` }
function date(value: string) { return new Date(value).toLocaleString('zh-CN', { hour12: false }) }
function terminal(task: Task) { return ['succeeded', 'failed', 'interrupted'].includes(task.status) }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : '操作失败，请重试' }
function report(error: unknown) { ElMessage.error(errorMessage(error)) }

async function refresh() {
  refreshing.value = true
  try {
    const result = await request(`/imports?offset=${(page.value - 1) * 20}&limit=20`)
    tasks.value = result.items; total.value = result.total; serviceError.value = ''
    if (!selected.value) {
      const saved = localStorage.getItem('askme-task')
      const initial = tasks.value.find(task => task.task_id === saved) || tasks.value[0]
      if (initial) select(initial)
    }
  } catch (error) { serviceError.value = errorMessage(error) }
  finally { refreshing.value = false }
}

function apply(task: Task) {
  if (selected.value?.task_id !== task.task_id) return
  if (new Date(task.updated_at).getTime() < new Date(selected.value.updated_at).getTime()) return
  selected.value = task
  const index = tasks.value.findIndex(item => item.task_id === task.task_id)
  if (index >= 0) tasks.value[index] = task
  if (task.message && eventLog.value.at(-1)?.message !== task.message) {
    eventLog.value.push({ time: new Date(task.updated_at).toLocaleTimeString('zh-CN', { hour12: false }), message: task.message })
    eventLog.value = eventLog.value.slice(-30)
  }
  if (terminal(task)) { stream?.close(); stream = null; streamState.value = '' }
}

function select(task: Task) {
  stream?.close(); stream = null; selected.value = task; eventLog.value = []; streamState.value = ''
  localStorage.setItem('askme-task', task.task_id)
  if (terminal(task)) return
  stream = progressStream(task.task_id)
  stream.onopen = () => { streamState.value = '实时连接已建立' }
  stream.onmessage = event => { try { apply(JSON.parse(event.data)) } catch { streamState.value = '进度格式异常，使用状态查询' } }
  stream.onerror = () => { streamState.value = '实时连接重连中，已启用状态查询' }
  stream.addEventListener('unavailable', () => { stream?.close(); stream = null; streamState.value = '实时连接已断开，使用状态查询' })
}

function choose(candidate?: File) {
  if (!candidate || uploading.value) return
  if (!/\.(pdf|md)$/i.test(candidate.name)) { ElMessage.warning('请选择 PDF 或 Markdown 文件'); return }
  if (candidate.size === 0 || candidate.size > auth.uploadLimit) { ElMessage.warning(`文件不能为空，且不能超过 ${size(auth.uploadLimit)}`); return }
  file.value = candidate; uploadProgress.value = 0
}
function onDrop(event: DragEvent) { dragging.value = false; choose(event.dataTransfer?.files[0]) }
function onFile(event: Event) { const input = event.target as HTMLInputElement; choose(input.files?.[0]); input.value = '' }

async function submit() {
  if (!file.value || uploading.value) return
  uploading.value = true
  try {
    const task = await uploadFile(file.value, percent => { uploadProgress.value = percent })
    page.value = 1; selected.value = null; await refresh(); select(task)
    ElMessage.success('文件已接收，请查看任务处理进度'); file.value = null
  } catch (error) { report(error) }
  finally { uploading.value = false }
}

async function retry() {
  if (!selected.value || retrying.value) return
  retrying.value = true
  try { const task = await request(`/imports/${selected.value.task_id}/retry`, 'POST'); select(task); await refresh() }
  catch (error) { report(error) }
  finally { retrying.value = false }
}

async function preview() {
  if (!selected.value) return
  previewVisible.value = true; previewBusy.value = true; previewSource.value = ''
  try { previewSource.value = await markdown(selected.value.task_id, variant.value) }
  catch (error) { report(error) }
  finally { previewBusy.value = false }
}

function download() {
  const object = URL.createObjectURL(new Blob([previewSource.value], { type: 'text/markdown;charset=utf-8' }))
  const anchor = document.createElement('a'); anchor.href = object; anchor.download = `${variant.value}.md`; anchor.click(); URL.revokeObjectURL(object)
}

async function logout() {
  try { await request('/auth/logout', 'POST'); auth.authenticated = false; auth.csrf = ''; await router.replace('/login') }
  catch (error) { report(error) }
}
watch(() => auth.authenticated, value => { if (!value) { stream?.close(); router.replace('/login') } })
watch(page, refresh)
onMounted(() => {
  refresh()
  poll = setInterval(async () => {
    if (!selected.value || terminal(selected.value)) return
    try { const task = await request(`/imports/${selected.value.task_id}`); apply(task) }
    catch (error) { if (error instanceof ApiError && error.status === 401) return; streamState.value = errorMessage(error) }
  }, 8000)
})
onUnmounted(() => { stream?.close(); if (poll) clearInterval(poll) })
</script>

<template>
  <div class="workspace">
    <aside class="sidebar">
      <a class="brand" href="/"><span class="brand-mark"><el-icon><Connection /></el-icon></span><strong>AskMe<span class="brand-dot">.</span></strong></a>
      <div class="workspace-label">PERSONAL WORKSPACE</div>
      <div class="space-card"><div class="space-avatar">我</div><div><strong>个人知识库</strong><small>让经历成为知识</small></div><span class="small-dot"></span></div>
      <div class="nav-label">工作台</div><nav><router-link class="nav-link active" to="/"><el-icon><FolderOpened /></el-icon>文档导入<span class="nav-shortcut">01</span></router-link></nav>
      <div class="sidebar-bottom"><div class="mini-network"><i></i><i></i><i></i><i></i></div><strong>每一份资料，都有价值。</strong><p>连接文档、图像与经验，<br>构建属于你的知识网络。</p><span class="subtle">AskMe Knowledge Studio</span></div>
    </aside>
    <main class="main-content">
      <header class="workspace-header"><div class="breadcrumb">工作台<el-icon><ArrowRight /></el-icon><span>文档导入</span></div><div class="header-actions"><span class="admin-badge"><span class="small-dot"></span>管理员</span><el-button text @click="logout" :icon="SwitchButton">退出登录</el-button></div></header>
      <div class="page-content">
        <section class="page-heading"><div><span class="eyebrow">KNOWLEDGE INGESTION</span><h1>让知识，从这里生长<span class="brand-dot">。</span></h1><p>导入你的资料，系统将理解内容、建立语义连接，并保留每一步处理过程。</p></div><span class="workspace-tag"><el-icon><Connection /></el-icon>个人知识工作台</span></section>
        <el-alert v-if="serviceError" :title="serviceError" type="error" show-icon :closable="false" class="service-alert" />
        <section class="overview-grid"><div class="overview-card"><span class="overview-icon violet"><el-icon><Files /></el-icon></span><div><small>全部导入任务</small><strong>{{ total }}<em>份</em></strong></div><span class="overview-detail">持续积累你的知识</span></div><div class="overview-card"><span class="overview-icon blue"><el-icon><CircleCheckFilled /></el-icon></span><div><small>当前页已完成</small><strong>{{ recentSucceeded }}<em>份</em></strong></div><span class="overview-detail">完整解析并校验入库</span></div><div class="overview-card"><span class="overview-icon mint"><el-icon><DataAnalysis /></el-icon></span><div><small>当前页知识分块</small><strong>{{ recentChunks }}<em>个</em></strong></div><span class="overview-detail">来自成功导入的文档</span></div></section>
        <div class="ingestion-grid">
          <section class="panel upload-panel"><div class="panel-title"><div><span class="section-number">01</span><h2>导入新文档</h2></div><span class="pill">PDF / MARKDOWN</span></div>
            <p class="panel-description">将简历、项目经历或技术资料添加到知识库。</p>
            <input ref="fileInput" class="hidden-input" type="file" accept=".pdf,.md" @change="onFile">
            <button class="drop-zone" :class="{ dragging, 'has-file': file }" :disabled="uploading" @click="fileInput?.click()" @dragover.prevent="dragging = true" @dragleave.prevent="dragging = false" @drop.prevent="onDrop">
              <span class="upload-symbol"><el-icon><Document v-if="file" /><UploadFilled v-else /></el-icon></span><strong>{{ file ? file.name : '拖拽文件到这里，或点击上传' }}</strong><span>{{ file ? size(file.size) + ' · 已准备好导入' : `支持 PDF 和 Markdown，单个文件不超过 ${size(auth.uploadLimit)}` }}</span><span v-if="!file" class="choose-label">选择文档<el-icon><ArrowRight /></el-icon></span>
            </button>
            <div v-if="uploading" class="upload-byte-progress"><span>文件上传 {{ uploadProgress }}%<small>上传完成后开始后台处理</small></span><el-progress :percentage="uploadProgress" :show-text="false" /></div>
            <el-button class="start-button" type="primary" size="large" :disabled="!file" :loading="uploading" @click="submit">{{ uploading ? '正在上传文档' : '开始导入知识库' }}<el-icon v-if="!uploading"><ArrowRight /></el-icon></el-button>
            <div class="pipeline-summary"><span><el-icon><Document /></el-icon>结构化解析</span><i></i><span><el-icon><Picture /></el-icon>图片理解</span><i></i><span><el-icon><DataAnalysis /></el-icon>语义入库</span></div><div class="upload-hint"><el-icon><CircleCheckFilled /></el-icon>原文与图片增强版本分别保留，便于追溯。</div>
          </section>
          <section class="panel progress-panel"><div class="panel-title"><div><span class="section-number">02</span><h2>导入处理进度</h2></div><span v-if="selected" class="status-badge" :class="selected.status"><i></i>{{ names[selected.status] }}</span><span v-else class="pill">等待文档</span></div>
            <template v-if="selected"><div class="current-file"><span class="file-icon"><el-icon><Document /></el-icon></span><div><strong :title="selected.filename">{{ selected.filename }}</strong><small>{{ size(selected.size) }} · 第 {{ selected.attempt }} 次处理</small></div><span class="subtle">{{ completed }}/{{ applicable.length }} 步</span></div>
              <div class="current-stage" :class="selected.status"><div><span class="stage-indicator"><el-icon v-if="selected.status === 'running'" class="is-loading"><Loading /></el-icon><el-icon v-else-if="selected.status === 'succeeded'"><CircleCheckFilled /></el-icon><el-icon v-else-if="selected.status === 'failed'"><WarningFilled /></el-icon><el-icon v-else><Clock /></el-icon></span><div><strong>{{ selected.message }}</strong><small>{{ streamState || (selected.status === 'queued' ? '任务已入队，等待 Worker 接收' : '每个阶段均保留处理结果') }}</small></div></div><span v-if="activeStep?.total">{{ activeStep.completed }} / {{ activeStep.total }}</span></div>
              <div class="steps-list"><div v-for="step in selected.steps" :key="step.id" class="step-row" :class="step.status"><span class="step-point"><el-icon v-if="step.status === 'succeeded'"><CircleCheckFilled /></el-icon><el-icon v-else-if="step.status === 'running'" class="is-loading"><Loading /></el-icon><el-icon v-else-if="step.status === 'failed'"><WarningFilled /></el-icon><span v-else-if="step.status === 'skipped'">—</span><i v-else></i></span><span class="step-label">{{ step.label }}</span><span class="step-state">{{ step.total && step.status === 'running' ? `${step.completed}/${step.total}` : names[step.status] }}</span></div></div>
              <el-alert v-if="selected.error" :title="selected.error.message" :description="`错误码：${selected.error.code}`" type="error" :closable="false" show-icon />
              <div v-for="warning in selected.warnings" :key="warning" class="task-warning">{{ warning }}</div>
              <div class="task-actions"><el-button v-if="['failed', 'interrupted'].includes(selected.status)" :icon="Refresh" :loading="retrying" @click="retry">重试任务</el-button><el-button v-if="selected.status === 'succeeded'" :icon="View" @click="preview">预览 Markdown</el-button><span v-if="selected.status === 'succeeded'" class="completion-meta">{{ selected.image_count }} 张图片 · {{ selected.chunk_count }} 个分块</span></div>
              <details v-if="eventLog.length" class="event-details"><summary>查看处理记录<span>{{ eventLog.length }} 条</span></summary><div v-for="(event, index) in eventLog" :key="index" class="event-line"><time>{{ event.time }}</time>{{ event.message }}</div></details>
            </template>
            <div v-else class="empty-progress"><div class="empty-network"><el-icon><Connection /></el-icon></div><h3>等待一份新的知识</h3><p>上传文档后，在这里查看解析、图片理解<br>和向量入库的每一步进展。</p><div class="empty-flow"><span>文档</span><i></i><span>理解</span><i></i><span>知识</span></div></div>
          </section>
        </div>
        <section class="panel history-panel"><div class="panel-title"><div><span class="section-number">03</span><h2>最近导入记录</h2><span class="count-chip">{{ total }}</span></div><el-button text :icon="Refresh" :loading="refreshing" @click="refresh">刷新记录</el-button></div><p class="panel-description">选择一条记录，查看处理状态与文档产物。</p>
          <el-table :data="tasks" @row-click="select" class="task-table" :row-class-name="({ row }: {row: Task}) => row.task_id === selected?.task_id ? 'selected-task' : ''"><el-table-column label="文档名称" min-width="240"><template #default="{ row }"><div class="table-filename"><span class="file-icon small"><el-icon><Document /></el-icon></span><div><strong>{{ row.filename }}</strong><small>{{ row.task_id.slice(0, 12) }}</small></div></div></template></el-table-column><el-table-column label="状态" width="120"><template #default="{ row }"><span class="status-badge" :class="row.status"><i></i>{{ names[row.status] }}</span></template></el-table-column><el-table-column label="文件大小" width="110"><template #default="{ row }">{{ size(row.size) }}</template></el-table-column><el-table-column label="知识分块" width="110"><template #default="{ row }">{{ row.status === 'succeeded' ? row.chunk_count : '—' }}</template></el-table-column><el-table-column label="导入时间" min-width="180"><template #default="{ row }">{{ date(row.created_at) }}</template></el-table-column><el-table-column label="" width="60"><template #default><el-icon><ArrowRight /></el-icon></template></el-table-column><template #empty><div class="empty-table">{{ serviceError ? '暂时无法获取记录，请检查服务后刷新' : '还没有导入记录，上传第一份资料开始吧' }}</div></template></el-table>
          <el-pagination v-if="total > 20" v-model:current-page="page" :total="total" :page-size="20" layout="prev, pager, next" />
        </section><footer class="workspace-footer"><span>AskMe · 让经历有迹可循</span><span>PERSONAL KNOWLEDGE STUDIO</span></footer>
      </div>
    </main>
    <el-dialog v-model="previewVisible" title="Markdown 文档预览" width="min(960px, 94vw)" class="preview-dialog"><div class="preview-tools"><el-radio-group v-model="variant" @change="preview"><el-radio-button value="original">原始版本</el-radio-button><el-radio-button value="enriched">图片增强版</el-radio-button></el-radio-group><el-button :disabled="!previewSource" @click="download">下载 Markdown</el-button></div><div v-loading="previewBusy" class="markdown-body" v-html="previewHtml"></div></el-dialog>
  </div>
</template>
