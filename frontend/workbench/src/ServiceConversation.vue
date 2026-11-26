<script setup lang="ts">
import {
  IconAttachment,
  IconDown,
  IconPlus,
} from '@arco-design/web-vue/es/icon'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type {
  Conversation,
  ConversationMessage,
  ModelSelection,
  ModelSelectionCatalog,
  TurnTokenUsage,
} from './api'
import StreamingText from './StreamingText.vue'

const props = defineProps<{
  customerName?: string
  conversation: Conversation | null
  messages: ConversationMessage[]
  busy: boolean
  sending: boolean
  pending: boolean
  tokenUsage: TurnTokenUsage | null
  modelOptions: ModelSelectionCatalog | null
}>()

const emit = defineEmits<{
  start: []
  refresh: []
  send: [content: string, selection: ModelSelection]
  refreshTurn: []
}>()

const draft = ref('')
const submittedText = ref('')
const messageList = ref<HTMLElement | null>(null)
const showScrollToBottom = ref(false)
const followLatestMessage = ref(true)
const activeQuestionIndex = ref(0)
const composerToolsVisible = ref(false)
const openModelMenu = ref<'conversation' | 'decision' | null>(null)
const modelMenu = ref<HTMLElement | null>(null)
const conversationModelProfile =
  ref<ModelSelection['conversation_model_profile']>('DEFAULT')
const decisionMode = ref<ModelSelection['decision_mode']>('LLM')
const attachmentNotice = ref('')
const animatedMessageIds = ref<string[]>([])
const replyArrived = ref(false)
const requestSentHere = ref(false)
const waitingForReply = computed(
  () =>
    props.pending ||
    (props.sending && requestSentHere.value && !replyArrived.value),
)

const customerQuestions = computed(() =>
  props.messages.filter((message) => message.role === 'CUSTOMER'),
)

const tokenUsageTooltip = computed(() => {
  const usage = props.tokenUsage
  if (!usage) {
    return '当前暂无可展示的 Token 用量'
  }
  return `当前上下文累计 ${usage.total_tokens} Token，占配置窗口 ${usage.used_percentage.toFixed(2)}%（输入 ${usage.input_tokens} / 输出 ${usage.output_tokens}）。`
})

const tokenUsageStyle = computed(() => {
  const percentage = Math.min(props.tokenUsage?.used_percentage ?? 0, 100)
  return { '--token-usage': `${percentage * 3.6}deg` }
})

watch(
  () => props.messages,
  async (messages, previous) => {
    const known = new Set((previous ?? []).map((item) => item.message_id))
    const incoming = messages.filter(
      (item) => item.role === 'ASSISTANT' && !known.has(item.message_id),
    )
    if (
      (previous?.length || props.sending || props.pending) &&
      incoming.length
    ) {
      animatedMessageIds.value.push(...incoming.map((item) => item.message_id))
      replyArrived.value = true
    }
    if (
      submittedText.value &&
      messages.some(
        (message) =>
          message.role === 'CUSTOMER' &&
          message.content === submittedText.value,
      )
    ) {
      draft.value = ''
      submittedText.value = ''
    }
    await nextTick()
    if (followLatestMessage.value) scrollToBottom('auto')
    updateScrollPosition()
  },
)

watch(
  () => props.conversation?.conversation_id,
  async () => {
    followLatestMessage.value = true
    activeQuestionIndex.value = 0
    animatedMessageIds.value = []
    requestSentHere.value = false
    replyArrived.value = false
    await nextTick()
    scrollToBottom('auto')
  },
)

/** 原消息入库后清空输入，但仍在此会话的消息区显示等待。 */
function submit() {
  const content = draft.value.trim()
  if (!content || props.sending || props.pending) return
  submittedText.value = content
  replyArrived.value = false
  requestSentHere.value = true
  emit('send', content, {
    conversation_model_profile: conversationModelProfile.value,
    decision_mode: decisionMode.value,
  })
}

/** 等待提示和逐字输出都跟随最新消息，但不打断正在翻看历史的客户。 */
async function followReply(): Promise<void> {
  await nextTick()
  if (followLatestMessage.value) scrollToBottom('auto')
}

watch(waitingForReply, followReply)

/** 展开或收起输入区的附加功能面板，并关闭另一侧选择面板。 */
function toggleComposerTools(): void {
  composerToolsVisible.value = !composerToolsVisible.value
  openModelMenu.value = null
}

/** 告知客户文件能力尚未接入，不打开系统文件选择器。 */
function showAttachmentUnavailable(): void {
  attachmentNotice.value = '文件与文件夹功能暂未接入，请在功能升级后重试。'
}

/** 分别展开对话模型或决策方式；两类选择互不覆盖。 */
function toggleModelMenu(kind: 'conversation' | 'decision'): void {
  openModelMenu.value = openModelMenu.value === kind ? null : kind
  composerToolsVisible.value = false
}

const selectedConversationModel = computed(
  () =>
    props.modelOptions?.conversation_models.find(
      (item) => item.value === conversationModelProfile.value,
    ) ?? null,
)
const selectedDecisionModel = computed(
  () =>
    props.modelOptions?.decision_models.find(
      (item) => item.value === decisionMode.value,
    ) ?? null,
)
const conversationModelButtonLabel = computed(
  () => selectedConversationModel.value?.label ?? '对话模型',
)
const decisionModelButtonLabel = computed(
  () => selectedDecisionModel.value?.label ?? 'LLM 意图决策',
)

watch(
  () => props.modelOptions,
  (options) => {
    if (!options) return
    conversationModelProfile.value = options.default_conversation_model
    decisionMode.value = options.default_decision_mode
  },
  { immediate: true },
)

function selectConversationModel(value: string): void {
  conversationModelProfile.value =
    value as ModelSelection['conversation_model_profile']
  openModelMenu.value = null
}

function selectDecisionMode(value: string): void {
  decisionMode.value = value as ModelSelection['decision_mode']
  openModelMenu.value = null
}

function closeModelMenu(event: PointerEvent): void {
  if (!modelMenu.value?.contains(event.target as Node)) {
    openModelMenu.value = null
  }
}

onMounted(() => document.addEventListener('pointerdown', closeModelMenu))
onBeforeUnmount(() =>
  document.removeEventListener('pointerdown', closeModelMenu),
)

/** 中文输入法选词时不发送；普通回车发送，Shift+Enter 保留为换行。 */
function handleComposerKeydown(event: KeyboardEvent) {
  if (event.key !== 'Enter' || event.shiftKey || event.isComposing) return
  event.preventDefault()
  submit()
}

/** 根据当前位置决定是否显示“回到底部”按钮。 */
function updateScrollPosition(): void {
  const element = messageList.value
  if (!element) return
  const distanceToBottom =
    element.scrollHeight - element.scrollTop - element.clientHeight
  followLatestMessage.value = distanceToBottom < 80
  showScrollToBottom.value = distanceToBottom >= 80
  updateActiveQuestion(element)
}

/** 根据消息在当前可视区域中的位置，高亮最接近阅读位置的客户问题。 */
function updateActiveQuestion(messageContainer: HTMLElement): void {
  const readingLine =
    messageContainer.getBoundingClientRect().top +
    messageContainer.clientHeight * 0.35
  let nearestIndex = 0
  customerQuestions.value.forEach((question, index) => {
    const questionElement = document.getElementById(
      `conversation-message-${question.message_id}`,
    )
    if (
      questionElement &&
      questionElement.getBoundingClientRect().top <= readingLine
    ) {
      nearestIndex = index
    }
  })
  activeQuestionIndex.value = nearestIndex
}

/** 滚动到最新消息；首次打开不播放动画，用户点击时平滑滚动。 */
function scrollToBottom(behavior: ScrollBehavior = 'smooth'): void {
  const element = messageList.value
  if (!element) return
  element.scrollTo({ top: element.scrollHeight, behavior })
  followLatestMessage.value = true
  showScrollToBottom.value = false
}

/** 将较长问题压缩成右侧定位列表中的短标题。 */
function questionPreview(content: string): string {
  const compact = content.replaceAll(/\s+/g, ' ').trim()
  return compact.length <= 12 ? compact : `${compact.slice(0, 12)}...`
}

/** 点击问题摘要后，把相应客户消息滚动到对话区中部。 */
function locateQuestion(messageId: string): void {
  const targetIndex = customerQuestions.value.findIndex(
    (question) => question.message_id === messageId,
  )
  if (targetIndex >= 0) activeQuestionIndex.value = targetIndex
  document
    .getElementById(`conversation-message-${messageId}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

const timeText = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
</script>

<template>
  <a-card class="conversation-card" :bordered="false">
    <template #title>
      <strong class="conversation-heading">在线咨询</strong>
    </template>
    <div v-if="conversation" class="conversation-live">
      <div class="conversation-scroll-region">
        <div
          ref="messageList"
          class="conversation-message-list"
          aria-live="polite"
          @scroll="updateScrollPosition"
        >
          <div v-if="!messages.length" class="conversation-empty">
            <h2>有什么可以帮您？</h2>
            <p>请在下方输入您想咨询的问题。</p>
          </div>
          <div
            v-for="message in messages"
            :id="`conversation-message-${message.message_id}`"
            :key="message.message_id"
            class="conversation-message"
            :class="{ 'from-customer': message.role === 'CUSTOMER' }"
          >
            <span class="message-sender">{{
              message.role === 'CUSTOMER'
                ? '我'
                : message.role === 'AGENT'
                  ? '人工客服'
                  : '客户服务'
            }}</span>
            <div class="message-bubble">
              <StreamingText
                :text="message.content"
                :animate="animatedMessageIds.includes(message.message_id)"
                @progress="followReply"
              />
            </div>
            <time>{{ timeText(message.created_at) }}</time>
          </div>
          <div
            v-if="waitingForReply"
            class="conversation-message assistant-waiting"
            role="status"
          >
            <span class="message-sender">客户服务</span>
            <div class="message-bubble conversation-pending">
              <span class="waiting-dots" aria-hidden="true"
                ><i /><i /><i
              /></span>
              <span>正在处理您的咨询…</span>
              <a-button
                v-if="pending"
                type="text"
                size="small"
                @click="$emit('refreshTurn')"
                >更新结果</a-button
              >
            </div>
          </div>
        </div>

        <button
          v-if="showScrollToBottom"
          class="scroll-to-bottom"
          type="button"
          aria-label="回到最新消息"
          @click="scrollToBottom()"
        >
          <IconDown />
        </button>

        <aside
          v-if="customerQuestions.length > 1"
          class="question-navigator"
          aria-label="本次咨询的问题定位"
        >
          <button
            v-for="(question, index) in customerQuestions"
            :key="question.message_id"
            type="button"
            class="question-navigator-marker"
            :class="{ 'is-active': activeQuestionIndex === index }"
            :aria-label="`定位到问题 ${index + 1}：${questionPreview(question.content)}`"
            @click="locateQuestion(question.message_id)"
          >
            <em>{{ questionPreview(question.content) }}</em>
            <span></span>
          </button>
        </aside>
      </div>
      <div class="conversation-composer">
        <a-textarea
          v-model="draft"
          :auto-size="{ minRows: 2, maxRows: 4 }"
          :max-length="2000"
          placeholder="请输入您的问题"
          :disabled="conversation.status !== 'ACTIVE'"
          @keydown="handleComposerKeydown"
        />
        <div class="composer-actions">
          <div class="composer-left-actions">
            <button
              type="button"
              class="composer-circle-button"
              :class="{ 'is-active': composerToolsVisible }"
              aria-label="添加内容"
              :aria-expanded="composerToolsVisible"
              @click="toggleComposerTools"
            >
              <IconPlus />
            </button>
            <span>请勿发送密码或验证码</span>
          </div>
          <div class="composer-right-actions">
            <span
              class="token-usage-help"
              :data-tooltip="tokenUsageTooltip"
              tabindex="0"
              :aria-label="tokenUsageTooltip"
            >
              <span
                class="token-usage-ring"
                :style="tokenUsageStyle"
                :title="tokenUsageTooltip"
              ></span>
            </span>
            <div ref="modelMenu" class="model-selection-controls">
              <div class="reasoning-mode-control">
                <button
                  type="button"
                  class="reasoning-mode-button"
                  :aria-expanded="openModelMenu === 'conversation'"
                  aria-label="选择对话模型"
                  @click="toggleModelMenu('conversation')"
                >
                  <small>对话</small>
                  <span>{{ conversationModelButtonLabel }}</span>
                  <IconDown />
                </button>
                <div
                  v-if="openModelMenu === 'conversation'"
                  class="reasoning-mode-panel conversation-model-panel"
                >
                  <section class="model-option-group">
                    <strong>对话模型</strong>
                    <button
                      v-for="item in modelOptions?.conversation_models ?? []"
                      :key="item.value"
                      type="button"
                      :class="{
                        'is-selected': item.value === conversationModelProfile,
                      }"
                      :disabled="!item.enabled"
                      :title="item.label"
                      @click="selectConversationModel(item.value)"
                    >
                      <span>{{ item.label }}</span>
                    </button>
                  </section>
                </div>
              </div>
              <div class="reasoning-mode-control">
                <button
                  type="button"
                  class="reasoning-mode-button"
                  :aria-expanded="openModelMenu === 'decision'"
                  aria-label="选择决策模型"
                  @click="toggleModelMenu('decision')"
                >
                  <small>决策</small>
                  <span>{{ decisionModelButtonLabel }}</span>
                  <IconDown />
                </button>
                <div
                  v-if="openModelMenu === 'decision'"
                  class="reasoning-mode-panel decision-model-panel"
                >
                  <section class="model-option-group">
                    <strong>决策模型</strong>
                    <button
                      v-for="item in modelOptions?.decision_models ?? []"
                      :key="item.value"
                      type="button"
                      :class="{ 'is-selected': item.value === decisionMode }"
                      :disabled="!item.enabled"
                      :title="item.description"
                      @click="selectDecisionMode(item.value)"
                    >
                      <span class="model-option-heading">
                        <span>{{ item.label }}</span>
                        <em>
                          {{ item.value === 'LLM' ? '实际路由' : '旁路观测' }}
                        </em>
                      </span>
                      <small>{{ item.description }}</small>
                    </button>
                  </section>
                </div>
              </div>
            </div>
            <a-button
              type="primary"
              :loading="sending"
              :disabled="
                !draft.trim() || pending || conversation.status !== 'ACTIVE'
              "
              @click="submit"
              >发送</a-button
            >
          </div>
        </div>
        <div v-if="composerToolsVisible" class="composer-tools-panel">
          <button type="button" @click="showAttachmentUnavailable">
            <IconAttachment />
            <span>
              <b>文件和文件夹</b>
              <small>暂未接入</small>
            </span>
          </button>
          <p v-if="attachmentNotice" role="status">{{ attachmentNotice }}</p>
        </div>
      </div>
    </div>
    <div v-else class="conversation-stage">
      <a-result
        status="info"
        title="有什么可以帮您？"
        subtitle="发起咨询后，我们会为您建立服务会话。"
      >
        <template #extra>
          <a-button
            type="primary"
            size="large"
            :disabled="!customerName"
            :loading="busy"
            @click="$emit('start')"
          >
            发起咨询
          </a-button>
        </template>
      </a-result>
    </div>
  </a-card>
</template>
