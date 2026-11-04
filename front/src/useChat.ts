/** 页面状态入口：mount→bootstrap/history→发送SSE→终态刷新；卸载停止自有连接。
 * 不缓存另一个IP的历史到localStorage。演示切换网络后重新从后端读取当前IP数据。
 */
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { json, stream, type Turn, type Snapshot, type Status, type Option, type Card } from './api'
import { createRequestId } from './requestId'

const questions = ['请介绍一下你的工作年限和 Agent 开发经历。','你最有代表性的 Agent 项目是什么？','你在项目中如何设计 RAG 检索流程？','你如何使用 LangChain 和 LangGraph？','你如何设计 Agent 的工具调用？','你如何处理多轮对话和上下文压缩？','你如何减少模型幻觉、保证回答有依据？','你如何评估 Agent 的效果？','你如何处理 Agent 的超时、重试和异常？','你如何优化 Agent 的响应速度和调用成本？']

/** Fisher-Yates无放回抽样，挂载时只执行一次；演示3条中不会重复，增量渲染不重抽。 */
function chooseThree() {
  const values = [...questions]
  for (let i=values.length-1; i>0; i--) {const j=Math.floor(Math.random()*(i+1)); [values[i],values[j]]=[values[j]!,values[i]!]}
  return values.slice(0,3)
}

export function useChat() {
  const turns=ref<Turn[]>([]), remaining=ref<number|null>(null), input=ref(''), error=ref(''), notice=ref('')
  const loading=ref(true), busy=ref(false), ready=ref(false), stages=ref<Record<string,string>>({}), suggestions=ref(chooseThree()), hasMore=ref(false)
  let controller: AbortController|null=null, requestId:string|null=null, poll:ReturnType<typeof setTimeout>|null=null, disposed=false
  const canSend=computed(()=>ready.value&&!busy.value&&!loading.value&&(remaining.value??0)>0)

  /** 从服务器恢复终态/活动轮；分页游标只用于更早历史，刷新不会重新发起生成。 */
  async function refresh(older=false) {
    const suffix=older&&turns.value.length?`?before=${encodeURIComponent(turns.value[0]!.id)}`:''
    const snapshot=await json<Snapshot>('/history'+suffix)
    turns.value=older?[...snapshot.items,...turns.value]:snapshot.items
    remaining.value=snapshot.remaining; hasMore.value=snapshot.has_more
    return snapshot.active_request
  }

  /** 同IP其他浏览器正在生成或本连接断开时轮询，终态停止；不发新的messages请求。 */
  function recover(id:string) {
    if (disposed) return
    busy.value=true; requestId=id
    poll=setTimeout(async()=>{
      try {
        const state=await json<Status>(`/requests/${id}`)
        remaining.value=state.remaining
        if (state.turn) {
          const index=turns.value.findIndex(t=>t.id===state.turn!.id)
          if (index>=0) turns.value[index]=state.turn; else turns.value.push(state.turn)
        }
        if (state.status==='running') recover(id)
        else {busy.value=false; requestId=null; stages.value={}}
      } catch(e) {error.value=message(e); busy.value=false; requestId=null}
    },1500)
  }

  /** 初始化失败仍显示可重试页面，不伪造回答/额度；配置补齐后点击重连即可。 */
  async function initialize() {
    loading.value=true; error.value=''
    try {
      await json('/bootstrap')
      const active=await refresh(); ready.value=true
      if(active) recover(active)
    } catch(e) {error.value=message(e); ready.value=false}
    finally {loading.value=false}
  }

  /** 提交普通问题或已绑定选项；meta绑定服务端turn，按事件更新而不重复添加消息。
   * 输入 text 为 1～2000 字问题，card/option 为澄清卡片及选择；返回 Promise<void>。
   * 顺序：输入校验 → requestId.ts 生成 UUID → 页面临时状态 → api.stream → 历史恢复。
   * 演示普通 HTTP 没有 randomUUID 时走安全随机字节回退，仍提交合法 request_id；
   * 浏览器连安全随机字节都不支持时，仅显示错误，不清空问题、不消费选项、不发送请求。
   */
  async function send(text=input.value, card?:Card, option?:Option) {
    if (busy.value||!ready.value||(!card&&!canSend.value)) return
    const question=text.trim()
    if(!question||question.length>2000) {error.value='请输入 1～2000 字的问题'; return}
    let id:string
    try {id=createRequestId()}
    catch(e) {
      error.value=message(e)
      // 仅记录固定故障类别，不输出输入文本、请求标识或浏览器全量状态。
      console.warn('[chat] request_id_generation_failed')
      return
    }
    const previous=card?turns.value.find(t=>t.clarification?.id===card.id):undefined
    // 一个提交只生成一次标识；pending 前缀区分 UI 临时轮次与服务端 turn_id。
    const local:Turn=previous??{id:'pending-'+id,question,answer:'',status:'running',citations:[],created_at:Date.now()/1000}
    if(previous) {previous.selection=question; previous.answer=''; previous.status='running'; previous.clarification!.consumed=true}
    else turns.value.push(local)
    input.value=''; error.value=''; notice.value=''; stages.value={}; busy.value=true
    requestId=id; controller=new AbortController()
    let finished=false, accepted=false
    try {
      const path=card?`/clarifications/${card.id}/select`:'/messages'
      const payload=card?{request_id:id,option_id:option!.id}:{request_id:id,message:question}
      await stream(path,payload,controller.signal,(event,data)=>{
        // turns内是Vue代理对象，必须按服务端ID/临时ID查找，不能只修改原始local引用。
        const turn=turns.value.find(t=>t.id===local.id)??turns.value.at(-1)!
        if(event==='meta') {accepted=true; turn.id=data.turn_id; local.id=data.turn_id; remaining.value=data.remaining}
        else if(event==='stage') stages.value={...stages.value,[data.stage]:data.status}
        else if(event==='notice') notice.value=data.message
        else if(event==='answer_delta') turn.answer+=data.text
        else if(event==='clarification') turn.clarification=data
        else if(event==='citations') turn.citations=data.items
        else if(event==='error') {error.value=data.message; turn.error=data.message}
        else if(event==='done') {finished=true; turn.status=data.status; remaining.value=data.remaining}
        else if(event==='snapshot') {finished=data.status!=='running'; if(data.turn) Object.assign(turn,data.turn); remaining.value=data.remaining}
      })
    } catch(e) {
      if(!(e instanceof DOMException&&e.name==='AbortError')) error.value=message(e)
      if(!accepted&&!card) input.value=question
    } finally {
      controller=null; busy.value=false; stages.value={}
      if(!disposed) {
        try {const active=await refresh(); if(active) recover(active); else requestId=null}
        catch {if(!finished) recover(id)}
      }
    }
  }

  /** compose仅回填，不触发免费续问；ask由后端校验IP、有效期和一次性消费。 */
  function select(card:Card, option:Option) {
    if(option.action==='compose') {input.value=option.value??''; return}
    void send(option.label,card,option)
  }

  /** 先请求服务端取消再中断浏览器读流；即使其他进程持有图也能读到取消标志。 */
  async function stop() {
    if(!requestId) return
    try {await json(`/requests/${requestId}/cancel`,'POST'); controller?.abort()}
    catch(e) {error.value=message(e)}
  }

  /** 错误只取Error.message，避免把任意响应对象或堆栈渲染到页面。 */
  function message(e:unknown) {return e instanceof Error?e.message:'连接暂时不可用，请重试'}
  onMounted(initialize)
  onUnmounted(()=>{disposed=true; controller?.abort(); if(poll) clearTimeout(poll)})
  return {turns,remaining,input,error,notice,loading,busy,ready,stages,suggestions,hasMore,canSend,send,select,stop,initialize,refresh}
}
