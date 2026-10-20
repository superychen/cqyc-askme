import { reactive } from 'vue'

export interface Step { id: string; label: string; status: string; completed: number; total: number | null; started_at?: string; finished_at?: string }
export interface Task {
  task_id: string; filename: string; status: string; created_at: string; updated_at: string;
  size: number; chunk_count: number; image_count: number; section_count: number; node: string | null;
  message: string; attempt: number; steps: Step[]; warnings: string[];
  error: null | { code: string; message: string; retryable: boolean; failed_node: string };
}
export const auth = reactive({ csrf: '', ready: false, authenticated: false, uploadLimit: 20 * 1024 * 1024 })
const base = '/api/v1/admin'

export class ApiError extends Error {
  constructor(message: string, public status: number, public code: string) { super(message) }
}

async function decode(response: Response) {
  const body = await response.json().catch(() => ({}))
  if (!response.ok) {
    if (response.status === 401) { auth.authenticated = false; auth.csrf = '' }
    throw new ApiError(body.message || '服务暂时不可用，请检查后端是否启动', response.status, body.code || 'REQUEST_FAILED')
  }
  return body
}

export async function request(path: string, method = 'GET', body?: unknown) {
  try {
    return await decode(await fetch(base + path, {
      method, credentials: 'same-origin', headers: {
        ...(body ? { 'Content-Type': 'application/json' } : {}),
        ...(method !== 'GET' && auth.csrf ? { 'X-CSRF-Token': auth.csrf } : {}),
      }, body: body ? JSON.stringify(body) : undefined,
    }))
  } catch (error) {
    if (error instanceof ApiError) throw error
    throw new ApiError('无法连接服务，请检查网络和后端进程', 0, 'NETWORK_ERROR')
  }
}

export async function restoreSession() {
  try {
    const session = await request('/auth/me')
    auth.csrf = session.csrf_token; auth.authenticated = true; auth.uploadLimit = session.upload_max_bytes
  } catch (error) {
    auth.authenticated = false
    if (!(error instanceof ApiError) || error.status !== 401) throw error
  } finally { auth.ready = true }
}

export function uploadFile(file: File, onProgress: (progress: number) => void): Promise<Task> {
  // XMLHttpRequest 提供上传字节进度；不能把上传完成误报为后台导入完成。
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', base + '/imports'); xhr.withCredentials = true
    xhr.setRequestHeader('X-CSRF-Token', auth.csrf)
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) onProgress(Math.round(event.loaded / event.total * 100)) }
    xhr.onload = () => {
      let result: any
      try { result = JSON.parse(xhr.responseText) } catch { reject(new Error('服务返回格式不正确')); return }
      if (xhr.status >= 200 && xhr.status < 300) resolve(result)
      else {
        if (xhr.status === 401) { auth.authenticated = false; auth.csrf = '' }
        reject(new ApiError(result.message || '上传失败', xhr.status, result.code))
      }
    }
    xhr.onerror = () => reject(new Error('网络连接中断，上传失败'))
    const data = new FormData(); data.append('file', file); xhr.send(data)
  })
}

export async function markdown(taskId: string, variant: string) {
  const response = await fetch(`${base}/imports/${taskId}/markdown?variant=${variant}`, { credentials: 'same-origin' })
  if (!response.ok) { await decode(response); return '' }
  return response.text()
}

export function progressStream(taskId: string) { return new EventSource(`${base}/imports/${taskId}/events`) }
