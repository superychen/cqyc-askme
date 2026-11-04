/** 公开接口边界：JSON用于历史/状态，POST SSE用于生成；不携带管理员 Token。
 * 演示同一 request_id 重试返回 snapshot，消费方恢复内容而不是重新启动生成。
 */
const base = '/api/v1/chat'

export interface Option {id: string; label: string; action: 'ask'|'compose'; value?: string}
export interface Card {id: string; message: string; options: Option[]; expires_at: number; consumed: boolean}
export interface Citation {number: number; title: string; text: string; url: string|null; source_type: string}
export interface Turn {id: string; question: string; selection?: string; answer: string; status: string; created_at: number; citations: Citation[]; clarification?: Card; error?: string}
export interface Snapshot {items: Turn[]; remaining: number; active_request: string|null; has_more: boolean}
export interface Status {turn: Turn|null; status: string; remaining: number}

/** 解码安全错误文案，响应不是JSON时也不能把内部HTML错误页原样展示。 */
async function checked(response: Response) {
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.message || `服务暂时不可用（${response.status}）`)
  }
  return response
}

/** 普通请求；演示 json('/history') 只读取本IP历史，不信任前端指定身份。 */
export async function json<T>(path: string, method = 'GET'): Promise<T> {
  const response = await checked(await fetch(base+path, {method, credentials: 'same-origin', cache: 'no-store'}))
  return response.json()
}

/** 流式解析UTF-8/SSE：read()可只有半个汉字、半行或多个事件，必须保留残余。
 * data多行以换行拼接，注释心跳忽略。AbortSignal由停止按钮/页面卸载控制。
 * 演示两个分片 'data: {"te' 与 'xt":"我"}\n\n' 仅触发一次完整事件。
 */
export async function stream(path: string, body: unknown, signal: AbortSignal, onEvent: (name: string, data: any) => void) {
  const response = await checked(await fetch(base+path, {method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body), signal}))
  if (!response.body || !response.headers.get('content-type')?.includes('text/event-stream')) throw new Error('服务未返回有效的对话流')
  const reader = response.body.getReader(), decoder = new TextDecoder()
  let buffer = '', event = 'message', data: string[] = []
  const line = (value: string) => {
    if (value === '') {
      if (data.length) onEvent(event, JSON.parse(data.join('\n')))
      event = 'message'; data = []
    } else if (value.startsWith('event:')) event = value.slice(6).trim()
    else if (value.startsWith('data:')) data.push(value.slice(5).replace(/^ /, ''))
  }
  try {
    while (true) {
      const result = await reader.read()
      buffer += decoder.decode(result.value, {stream: !result.done})
      if (buffer.length > 1024*1024) throw new Error('对话流数据过大')
      let end: number
      while ((end = buffer.indexOf('\n')) >= 0) {
        line(buffer.slice(0, end).replace(/\r$/, '')); buffer = buffer.slice(end+1)
      }
      if (result.done) break
    }
    // 正常SSE以空行结尾；残缺帧不能作为完整回答，后续通过快照恢复。
    if (buffer.trim() || data.length) throw new Error('对话连接中断，正在恢复已保存内容')
  } finally { await reader.cancel().catch(() => {}); reader.releaseLock() }
}
