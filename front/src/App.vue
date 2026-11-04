<script setup lang="ts">
/** 公开页面：展示求职者AI分身，不写入未提供的真实履历；业务状态由useChat管理。 */
import { computed, nextTick, ref, watch } from 'vue'
import MarkdownIt from 'markdown-it'
import DOMPurify from 'dompurify'
import { useChat } from './useChat'
import type { Card } from './api'

const chat=useChat()
const {turns,remaining,input,error,notice,loading,busy,ready,stages,suggestions,hasMore,canSend}=chat
const timeline=ref<HTMLElement|null>(null), composer=ref<HTMLTextAreaElement|null>(null), following=ref(true)
const renderer=new MarkdownIt({html:false,linkify:false,breaks:true})
// 回答里的图片只显示说明，不能通过远程图片向第三方泄漏访客IP。
renderer.renderer.rules.image=(tokens,index)=>renderer.utils.escapeHtml(tokens[index]?.content??'')
const stageText:Record<string,string>={load_context:'读取对话上下文',prepare_query:'理解你的问题',web_route:'准备检索',rag_retrieve:'查阅个人资料',web_search:'检索技术资料',retrieval_join:'汇总检索结果',fuse_candidates:'融合候选资料',rerank:'筛选相关证据',evidence_gate:'核对回答依据',answer:'正在回答',clarify:'整理可追问方向',unknown:'整理回答',persist_turn:'保存本轮对话',compact_context:'整理对话记忆'}
const activeStages=computed(()=>Object.entries(stages.value).filter(([,v])=>v==='running').map(([k])=>stageText[k]??'正在处理'))
const countLabel=computed(()=>remaining.value===null?'20 次对话体验':`剩余 ${remaining.value} / 20 次`)

/** Markdown先关闭HTML再清洗；演示script文本不会执行，外链仅允许HTTP(S)。 */
function render(text:string) {return DOMPurify.sanitize(renderer.render(text),{FORBID_TAGS:['img','iframe','form','style','script'],FORBID_ATTR:['style','srcset'],ALLOWED_URI_REGEXP:/^https?:\/\//i})}
/** 卡片已消费/超时不得继续免费调用；服务器始终再次校验，前端仅改善反馈。 */
function expired(card:Card) {return card.consumed||card.expires_at*1000<Date.now()}
/** 中文输入法组合键不发送；Shift+Enter换行，普通Enter才提交。 */
function keydown(event:KeyboardEvent) {if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault(); void chat.send()}}
/** 仅用户仍停留底部时跟随输出，上滑阅读引用时不抢滚动位置。 */
function scroll() {const el=timeline.value; if(el) following.value=el.scrollHeight-el.scrollTop-el.clientHeight<100}
watch(()=>turns.value.map(t=>t.answer.length+(t.clarification?1:0)).join(','),async()=>{await nextTick();if(following.value&&timeline.value) timeline.value.scrollTop=timeline.value.scrollHeight})
/** 输入区高度限制在140px，长问题继续滚动，避免挤掉整个聊天区域。 */
watch(input,async()=>{await nextTick();if(composer.value){composer.value.style.height='auto';composer.value.style.height=Math.min(composer.value.scrollHeight,140)+'px'}})
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <a class="brand" href="/" aria-label="Ask Me 首页"><span class="brand-icon">a<span>·</span></span><span>ask me<span class="brand-dot">.</span></span></a>
      <div class="header-right"><span class="mode"><i></i> Agent Developer</span><span class="quota">{{countLabel}}</span></div>
    </header>
    <main class="workspace">
      <section class="conversation" ref="timeline" @scroll="scroll" aria-label="对话记录">
        <div v-if="!turns.length" class="welcome">
          <div class="eyebrow"><span class="mini-star">✳</span> MORE THAN A RÉSUMÉ</div>
          <h1>从一句提问，<br>了解我的 <span>Agent 实践。</span></h1>
          <p class="intro">关于我的经历、项目与技术思路，<br class="mobile-break">我们可以直接聊聊。</p>
          <div class="welcome-note"><span class="note-icon">↗</span><div><strong>一份可以对话的个人资料</strong><p>基于本人上传的简历与项目文档回答，<br>没有依据的事情，我会坦诚说不知道。</p></div></div>
          <div class="topic-labels"><span>项目经历</span><span>RAG & 工具调用</span><span>工程实践</span></div>
        </div>
        <div v-else class="messages">
          <button v-if="hasMore" class="load-more" @click="chat.refresh(true).catch(()=>error='暂时无法加载更早的对话')">查看更早的对话 ↑</button>
          <div class="conversation-note">同一公网 IP 共享历史与体验额度</div>
          <article v-for="turn in turns" :key="turn.id" class="turn">
            <div class="question"><span class="speaker">你</span><p>{{turn.question}}</p></div>
            <div v-if="turn.selection" class="selection-note">继续了解：{{turn.selection}}</div>
            <div class="answer"><div class="answer-heading"><span class="avatar">a·</span><strong>Agent 开发者</strong><span>AI 分身</span></div>
              <div v-if="turn.answer" class="markdown" v-html="render(turn.answer)"></div>
              <div v-if="turn.clarification&&!turn.answer" class="clarification">
                <p>{{turn.clarification.message}}</p>
                <div class="clarification-options"><button v-for="(option,index) in turn.clarification.options" :key="option.id" :disabled="busy||expired(turn.clarification)" @click="chat.select(turn.clarification!,option)"><span>0{{index+1}}</span>{{option.label}}<b>↗</b></button></div>
                <small>{{expired(turn.clarification)?'这些选项已失效，你可以在下方继续提问。':'选择一个方向继续，或补充更具体的问题。'}}</small>
              </div>
              <div v-if="turn.status==='running'&&!turn.answer&&!turn.clarification" class="thinking"><i></i><i></i><i></i></div>
              <details v-if="turn.citations.length" class="sources"><summary>回答依据 <span>{{turn.citations.length}} 份资料</span></summary><div v-for="source in turn.citations" :key="source.number" class="source"><a v-if="source.url" :href="source.url" target="_blank" rel="noopener noreferrer">[{{source.number}}] {{source.title}} ↗</a><strong v-else>[{{source.number}}] {{source.title}}</strong><p>{{source.text}}</p></div></details>
              <p v-if="turn.error" class="turn-error">{{turn.error}}</p>
              <span v-if="turn.status==='interrupted'" class="interrupted">回答已中断 · 已保留收到的内容</span>
            </div>
          </article>
        </div>
      </section>
      <section class="composer-area" aria-label="提问">
        <div v-if="error" class="alert" role="alert"><span>{{error}}</span><button v-if="!ready" @click="chat.initialize">重新连接</button><button v-else @click="error=''" aria-label="关闭提示">×</button></div>
        <div v-if="notice" class="notice" role="status">{{notice}}</div>
        <div v-if="busy" class="stage-row" role="status"><span class="pulse"></span>{{activeStages.join(' · ')||'正在同步对话…'}}</div>
        <div class="suggestions"><span class="suggestion-label">可以这样问</span><button v-for="question in suggestions" :key="question" :disabled="!canSend" @click="chat.send(question)">{{question}}<span>↗</span></button></div>
        <form class="composer" :class="{'is-busy':busy}" @submit.prevent="chat.send()">
          <textarea ref="composer" v-model="input" :disabled="loading||!ready" maxlength="2000" rows="1" aria-label="输入面试问题" :placeholder="remaining===0?'当前网络的体验额度已用完':'想了解什么？从这里开始…'" @keydown="keydown"></textarea>
          <div class="composer-bottom"><span><span class="input-star">✳</span> 关于我，也关于 Agent <small v-if="input.length">· {{input.length}} / 2000</small></span><button v-if="busy" type="button" class="stop" @click="chat.stop" aria-label="停止回答">■</button><button v-else type="submit" class="send" :disabled="!canSend||!input.trim()" aria-label="发送问题">↑</button></div>
        </form>
        <div class="footnote"><span>AI 分身基于资料回答，请以实际沟通为准。</span><span>同一网络共享 20 次体验</span></div>
      </section>
    </main>
    <footer class="site-footer"><span>BUILT AROUND CONVERSATION</span><a href="/admin/">管理入口 ↗</a></footer>
  </div>
</template>
