<script setup lang="ts">
import { IconDown, IconLeft, IconRight } from '@arco-design/web-vue/es/icon'
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import CustomerDetails from './CustomerDetails.vue'
import StreamingText from './StreamingText.vue'
import type {
  Conversation,
  ConversationMessage,
  ConversationSummary,
  ConversationWrapUp,
  CustomerServiceContext,
  CustomerProfile,
  CustomerWorkload,
  Recommendation,
  RagReview,
  SopAssistance,
  SopReview,
} from './api'

const props = defineProps<{
  customers: CustomerProfile[]
  customerWorkloads: CustomerWorkload[]
  selectedCustomerId: string
  context: CustomerServiceContext | null
  contextLoading: boolean
  busy: boolean
  conversations: ConversationSummary[]
  conversation: Conversation | null
  messages: ConversationMessage[]
  recommendations: Recommendation[]
  wrapUp: ConversationWrapUp | null
  sopAssistance: SopAssistance | null
  sendingAssistance: boolean
  reviewingSop: boolean
  reviewingRag: boolean
  pendingCustomerIds: string[]
  pendingConversationIds: string[]
}>()

const emit = defineEmits<{
  select: [customerId: string]
  selectConversation: [conversationId: string]
  recommendationAction: [recommendationId: string, action: 'VIEWED' | 'COPIED']
  recommendationFeedback: [recommendationId: string, helpful: boolean]
  createWrapUp: []
  confirmWrapUp: []
  sendAssistance: [content: string]
  reviewSop: [payload: SopReview]
  reviewRag: [payload: RagReview]
}>()

const search = ref('')
const page = ref(1)
const pageSize = 8
const activePanel = ref('customer')
const processPanelVisible = ref(false)
const assistanceDraft = ref('')
const identityVerified = ref(false)
const correctionVisible = ref(false)
const correctedFields = ref<Record<string, string>>({})
const animatedMessageIds = ref<string[]>([])
const ragEditedAnswers = ref<Record<string, string>>({})
const ragReviewNotes = ref<Record<string, string>>({})
const reviewClock = ref(Date.now())
let reviewClockTimer: ReturnType<typeof setInterval> | undefined

onMounted(() => {
  reviewClockTimer = setInterval(() => {
    reviewClock.value = Date.now()
  }, 30_000)
})

onUnmounted(() => {
  if (reviewClockTimer) clearInterval(reviewClockTimer)
})

/** 即使页面一直打开，也按服务端下发的截止时间停止发送旧草稿。 */
function reviewStatus(item: Recommendation): Recommendation['review_status'] {
  if (
    item.review_status === 'DRAFT' &&
    item.review_expires_at &&
    Date.parse(item.review_expires_at) <= reviewClock.value
  ) {
    return 'EXPIRED'
  }
  return item.review_status
}

/** 审核时限是旧草稿能否发送的边界，不是运行产物的保留时间。 */
function reviewDeadlineText(value?: string | null): string {
  return value ? `审核截止：${timeText(value)}` : ''
}

/** 审批变化或切换流程时重新显示已保存资料，不把旧核验勾选带到另一项审批。 */
watch(
  () => [
    props.sopAssistance?.sop_execution_id,
    props.sopAssistance?.approval_id,
  ],
  () => {
    identityVerified.value = false
    correctionVisible.value = false
    correctedFields.value = Object.fromEntries(
      (props.sopAssistance?.editable_fields ?? []).map((field) => [
        field.name,
        field.value,
      ]),
    )
  },
  { immediate: true },
)

/** 草稿更新时保留坐席尚未提交的编辑，只初始化新出现的草稿。 */
watch(
  () => props.recommendations,
  (items) => {
    for (const item of items) {
      if (!['DRAFT', 'EXPIRED'].includes(item.review_status)) continue
      if (item.review_status === 'DRAFT') {
        ragEditedAnswers.value[item.recommendation_id] ??= item.answer
      }
      ragReviewNotes.value[item.recommendation_id] ??= ''
    }
  },
  { immediate: true },
)

/** 浏览器只提交动作和标识；后端会重新读取权威草稿并核对图停点。 */
function submitRagReview(
  item: Recommendation,
  action: RagReview['action'],
): void {
  if (props.reviewingRag || !item.rag_draft_id) return
  emit('reviewRag', {
    recommendation_id: item.recommendation_id,
    turn_id: item.turn_id,
    draft_id: item.rag_draft_id,
    action,
    edited_answer:
      action === 'EDIT'
        ? (ragEditedAnswers.value[item.recommendation_id] ?? '').trim()
        : null,
    note: (ragReviewNotes.value[item.recommendation_id] ?? '').trim(),
  })
}

/** 只提交确实修改的字段，旧确认由后端统一作废，不能在页面直接批准更正值。 */
const changedFields = computed(() =>
  Object.fromEntries(
    (props.sopAssistance?.editable_fields ?? [])
      .filter((field) => correctedFields.value[field.name] !== field.value)
      .map((field) => [field.name, correctedFields.value[field.name]]),
  ),
)

/** 通过明确的动作按钮提交处理请求，和聊天意见分开，操作者由登录身份决定。 */
function submitReview(action: SopReview['action']): void {
  const view = props.sopAssistance
  if (!view || props.reviewingSop || view.processing) return
  emit('reviewSop', {
    sop_execution_id: view.sop_execution_id,
    turn_id: view.turn_id,
    approval_id: view.approval_id ?? null,
    action,
    identity_verified: identityVerified.value,
    corrections: action === 'CORRECT' ? changedFields.value : {},
    note: assistanceDraft.value.trim(),
  })
}

/** 切换业务流程时清空旧草稿，处理页始终由坐席主动点击打开。 */
watch(
  () => props.sopAssistance?.sop_execution_id,
  () => {
    assistanceDraft.value = ''
    processPanelVisible.value = false
  },
)

/** 切换会话时取消旧动画和草稿，不重新播放历史消息。 */
watch(
  () => props.conversation?.conversation_id,
  () => {
    animatedMessageIds.value = []
    assistanceDraft.value = ''
    processPanelVisible.value = false
  },
)

/** 点击暂停提示中的入口，查看当前会话的流程资料和人工意见输入区。 */
function openProcessPanel(): void {
  if (props.sopAssistance) processPanelVisible.value = true
}

/** 返回原来的三个资料标签，保留之前选中的标签和尚未发送的意见。 */
function closeProcessPanel(): void {
  processPanelVisible.value = false
}

/** 普通回车发送处理意见；换行和中文输入法选词不触发发送。 */
function submitAssistance(event?: KeyboardEvent): void {
  if (event && (event.key !== 'Enter' || event.shiftKey || event.isComposing))
    return
  event?.preventDefault()
  if (
    assistanceDraft.value.trim() &&
    !props.sendingAssistance &&
    !props.sopAssistance?.processing
  )
    emit('sendAssistance', assistanceDraft.value.trim())
}

watch(
  () => props.messages,
  (items, previous) => {
    const known = new Set((previous ?? []).map((item) => item.message_id))
    if (previous?.length)
      animatedMessageIds.value.push(
        ...items
          .filter(
            (item) => item.role === 'ASSISTANT' && !known.has(item.message_id),
          )
          .map((item) => item.message_id),
      )
    if (
      items.some(
        (item) =>
          item.role === 'AGENT' &&
          !known.has(item.message_id) &&
          item.content === assistanceDraft.value.trim(),
      )
    )
      assistanceDraft.value = ''
  },
)
const showConversationList = ref(false)
const conversationFilter = ref<'ALL' | 'ACTIVE' | 'WRAP_UP'>('ALL')
const collapsedSessionGroups = ref<string[]>([])
const messageList = ref<HTMLElement | null>(null)
const showScrollToBottom = ref(false)
const customerQuestions = computed(() =>
  props.messages.filter((message) => message.role === 'CUSTOMER'),
)
const activeQuestionIndex = ref(0)
const workloadByCustomer = computed(
  () =>
    new Map(props.customerWorkloads.map((item) => [item.customer_id, item])),
)

/** 优先待确认/待核对，其次仅进行中，最后全部结束或尚无会话；同层保持原列表顺序。 */
function customerPriority(customerId: string): number {
  const workload = workloadByCustomer.value.get(customerId)
  return workload?.waiting_count ? 0 : workload?.open_count ? 1 : 2
}

/** 徽标统计的是未结束会话数，不是未读消息数；色彩取该客户最高待办优先级。 */
function customerBadge(customerId: string) {
  const workload = workloadByCustomer.value.get(customerId)
  return {
    count: workload?.open_count ?? 0,
    color:
      customerPriority(customerId) === 0
        ? 'waiting'
        : customerPriority(customerId) === 1
          ? 'active'
          : 'finished',
  }
}
const customerGroupNames = ['待确认 / 待核对', '进行中', '已结束 / 暂无咨询']
const collapsedCustomerGroups = ref<number[]>([])

/** 收起某一优先级的客户组，不改变客户排序、分页或中央选中的会话。 */
function toggleCustomerGroup(priority: number): void {
  collapsedCustomerGroups.value = collapsedCustomerGroups.value.includes(
    priority,
  )
    ? collapsedCustomerGroups.value.filter((value) => value !== priority)
    : [...collapsedCustomerGroups.value, priority]
}
const filteredCustomers = computed(() => {
  const keyword = search.value.trim()
  const items = keyword
    ? props.customers.filter((item) => item.display_name.includes(keyword))
    : props.customers
  return [...items].sort(
    (a, b) => customerPriority(a.customer_id) - customerPriority(b.customer_id),
  )
})
const pagedCustomers = computed(() =>
  filteredCustomers.value.slice(
    (page.value - 1) * pageSize,
    page.value * pageSize,
  ),
)

/** 把当前页客户按优先级分成三个固定分组；空分组也保留标题，避免列表状态随数据跳动。 */
const customerGroups = computed(() =>
  customerGroupNames.map((name, priority) => ({
    name,
    priority,
    items: pagedCustomers.value.filter(
      (item) => customerPriority(item.customer_id) === priority,
    ),
  })),
)

watch(search, () => {
  page.value = 1
})

const statusText: Record<string, string> = {
  ACTIVE: '进行中',
  WRAP_UP: '待确认',
  FINISHED: '已结束',
}

const timeText = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))

/** 把会话更新时间格式化为会话面板的日期分组标题。 */
const dayText = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  }).format(new Date(value))

const currentSummary = computed(() =>
  props.conversations.find(
    (item) => item.conversation_id === props.conversation?.conversation_id,
  ),
)

const selectedCustomer = computed(() =>
  props.customers.find((item) => item.customer_id === props.selectedCustomerId),
)

const sessionGroups = computed(() => {
  const groups = new Map<string, ConversationSummary[]>()
  for (const item of props.conversations) {
    const key = new Intl.DateTimeFormat('zh-CN', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
    }).format(new Date(item.last_message_at ?? item.created_at))
    const items = groups.get(key) ?? []
    items.push(item)
    groups.set(key, items)
  }
  return [...groups.entries()].map(([key, items]) => ({
    key,
    label: dayText(items[0].last_message_at ?? items[0].created_at),
    items,
  }))
})

/** 每条会话使用自己的待核对标记，切换选中项不改变任何条目的状态标签。 */
function needsManualReview(item: ConversationSummary): boolean {
  return item.status === 'ACTIVE' && (item.needs_manual_review ?? false)
}

// “待确认”是人工待办入口，包含小结待确认和 SOP 待核对两种情况。
const visibleConversations = computed(() =>
  conversationFilter.value === 'ALL'
    ? props.conversations
    : props.conversations.filter((item) =>
        conversationFilter.value === 'ACTIVE'
          ? item.status === 'ACTIVE' && !needsManualReview(item)
          : item.status === 'WRAP_UP' || needsManualReview(item),
      ),
)

/** 展开或收起坐席左侧某一天的会话，和客户页保持相同交互。 */
function toggleSessionGroup(groupKey: string) {
  collapsedSessionGroups.value = collapsedSessionGroups.value.includes(groupKey)
    ? collapsedSessionGroups.value.filter((key) => key !== groupKey)
    : [...collapsedSessionGroups.value, groupKey]
}

/** 判断某个日期分组是否已收起。 */
function isSessionGroupCollapsed(groupKey: string) {
  return collapsedSessionGroups.value.includes(groupKey)
}

/** 依据消息区当前位置显示回到底部按钮，并更新右侧问题标记。 */
function updateMessagePosition() {
  const element = messageList.value
  if (!element) return
  const distance =
    element.scrollHeight - element.scrollTop - element.clientHeight
  showScrollToBottom.value = distance >= 80
  const readingLine =
    element.getBoundingClientRect().top + element.clientHeight * 0.35
  let nearest = 0
  customerQuestions.value.forEach((question, index) => {
    const target = document.getElementById(
      `agent-message-${question.message_id}`,
    )
    if (target && target.getBoundingClientRect().top <= readingLine)
      nearest = index
  })
  activeQuestionIndex.value = nearest
}

/** 滚动到当前会话最新消息。 */
function scrollToLatest() {
  messageList.value?.scrollTo({
    top: messageList.value.scrollHeight,
    behavior: 'smooth',
  })
  showScrollToBottom.value = false
}

/** 将坐席消息区定位到指定客户问题。 */
function locateQuestion(messageId: string) {
  const index = customerQuestions.value.findIndex(
    (item) => item.message_id === messageId,
  )
  if (index >= 0) activeQuestionIndex.value = index
  document
    .getElementById(`agent-message-${messageId}`)
    ?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

/** 生成右侧悬停时显示的客户问题摘要。 */
function questionPreview(content: string) {
  const compact = content.replaceAll(/\s+/g, ' ').trim()
  return compact.length <= 12 ? compact : `${compact.slice(0, 12)}...`
}

watch(
  () => [props.messages, props.conversation?.conversation_id],
  async () => {
    await nextTick()
    updateMessagePosition()
  },
)

/** 把当前客户切换为会话列表，中央窗口仍保持当前选中的会话。 */
function openCustomerSessions(customerId: string) {
  showConversationList.value = true
  emit('select', customerId)
}

/** 返回客户列表，避免把会话切换做成中央窗口右上角的隐蔽下拉框。 */
function backToCustomers() {
  showConversationList.value = false
}

/** 切换中央消息窗口到坐席明确点选的会话。 */
function selectSession(conversationId: string) {
  emit('selectConversation', conversationId)
}

async function copyRecommendation(recommendation: Recommendation) {
  /** 复制后只记录客观操作，不自动推断坐席已经采纳建议。 */
  await navigator.clipboard.writeText(recommendation.answer)
  emit('recommendationAction', recommendation.recommendation_id, 'COPIED')
}
</script>

<template>
  <a-layout class="workspace-layout agent-workspace">
    <a-layout-sider :width="272" class="workspace-sider">
      <a-card
        v-if="!showConversationList"
        class="fill-card customer-directory"
        :bordered="false"
      >
        <template #title>客户列表</template>
        <template #extra
          ><a-tag>{{ customers.length }} 位</a-tag></template
        >
        <a-input-search
          v-model="search"
          allow-clear
          placeholder="搜索客户姓名"
          class="customer-search"
        />
        <a-list
          :bordered="false"
          :split="false"
          :loading="busy && !customers.length"
          size="small"
          class="customer-directory-list"
        >
          <a-list-item
            v-for="group in customerGroups"
            :key="`customer-group-${group.priority}`"
            class="customer-group-entry"
          >
            <button
              class="customer-group-heading"
              type="button"
              :aria-expanded="!collapsedCustomerGroups.includes(group.priority)"
              @click="toggleCustomerGroup(group.priority)"
            >
              <span>{{ group.name }}</span>
              <IconDown
                :class="{
                  'is-collapsed': collapsedCustomerGroups.includes(
                    group.priority,
                  ),
                }"
              />
            </button>
            <template v-if="!collapsedCustomerGroups.includes(group.priority)">
              <button
                v-for="item in group.items"
                :key="item.customer_id"
                class="customer-entry"
                :class="{
                  'is-selected': selectedCustomerId === item.customer_id,
                }"
                :aria-pressed="selectedCustomerId === item.customer_id"
                @click="openCustomerSessions(item.customer_id)"
              >
                <a-avatar :size="36">{{
                  item.display_name.slice(0, 1)
                }}</a-avatar>
                <span class="customer-entry-text">
                  <strong>{{ item.display_name }}</strong>
                  <small>{{ item.masked_contact }}</small>
                </span>
                <i
                  v-if="pendingCustomerIds.includes(item.customer_id)"
                  class="session-new-dot"
                  aria-label="有新消息"
                />
                <span
                  class="customer-conversation-badge"
                  :class="customerBadge(item.customer_id).color"
                  :aria-label="`${customerBadge(item.customer_id).count} 个未结束会话`"
                  :title="`${customerBadge(item.customer_id).count} 个未结束会话`"
                  >{{ customerBadge(item.customer_id).count }}</span
                >
              </button>
              <span v-if="!group.items.length" class="customer-group-empty"
                >暂无客户</span
              >
            </template>
          </a-list-item>
        </a-list>
        <a-empty
          v-if="!filteredCustomers.length && !busy"
          :description="search ? '没有找到客户' : '暂无服务客户'"
          class="directory-empty"
        />
        <a-pagination
          v-if="filteredCustomers.length > pageSize"
          v-model:current="page"
          :total="filteredCustomers.length"
          :page-size="pageSize"
          size="small"
          class="customer-pagination"
        />
      </a-card>

      <a-card
        v-else
        class="fill-card customer-directory agent-session-directory"
        :bordered="false"
      >
        <template #title>
          <div class="agent-session-list-title">
            <a-button
              type="text"
              size="small"
              aria-label="返回客户列表"
              @click="backToCustomers"
              >←</a-button
            >
            <strong>会话列表</strong>
          </div>
        </template>
        <template #extra
          ><span class="agent-session-customer-name">{{
            selectedCustomer?.display_name
          }}</span></template
        >
        <div class="agent-session-filter">
          <a-button
            size="small"
            :type="conversationFilter === 'ALL' ? 'primary' : 'secondary'"
            @click="conversationFilter = 'ALL'"
            >全部</a-button
          >
          <a-button
            size="small"
            :type="conversationFilter === 'ACTIVE' ? 'primary' : 'secondary'"
            @click="conversationFilter = 'ACTIVE'"
            >进行中</a-button
          >
          <a-button
            size="small"
            :type="conversationFilter === 'WRAP_UP' ? 'primary' : 'secondary'"
            @click="conversationFilter = 'WRAP_UP'"
            >待确认</a-button
          >
        </div>
        <div class="agent-session-list">
          <template v-for="group in sessionGroups" :key="group.key">
            <template
              v-if="
                group.items.some((item) => visibleConversations.includes(item))
              "
            >
              <button
                type="button"
                class="agent-session-day-toggle"
                :aria-expanded="!isSessionGroupCollapsed(group.key)"
                @click="toggleSessionGroup(group.key)"
              >
                <span>{{ group.label }}</span
                ><IconDown
                  :class="{
                    'is-collapsed': isSessionGroupCollapsed(group.key),
                  }"
                />
              </button>
              <div v-show="!isSessionGroupCollapsed(group.key)">
                <button
                  v-for="item in group.items.filter((entry) =>
                    visibleConversations.includes(entry),
                  )"
                  :key="item.conversation_id"
                  class="agent-session-entry"
                  :class="{
                    'is-selected':
                      item.conversation_id === conversation?.conversation_id,
                  }"
                  @click="selectSession(item.conversation_id)"
                >
                  <span class="agent-session-entry-main"
                    ><strong>{{ item.title }}</strong
                    ><small>{{
                      timeText(item.last_message_at ?? item.created_at)
                    }}</small></span
                  >
                  <span class="agent-session-entry-side"
                    ><i
                      v-if="
                        pendingConversationIds.includes(item.conversation_id)
                      "
                      class="session-new-dot"
                    /><a-tag
                      v-if="needsManualReview(item)"
                      size="small"
                      color="orange"
                      >待核对</a-tag
                    ><a-tag
                      v-else
                      size="small"
                      :color="
                        item.status === 'ACTIVE'
                          ? 'arcoblue'
                          : item.status === 'WRAP_UP'
                            ? 'orange'
                            : 'gray'
                      "
                      >{{ statusText[item.status] ?? item.status }}</a-tag
                    ></span
                  >
                </button>
              </div>
            </template>
          </template>
          <a-empty
            v-if="!visibleConversations.length"
            description="暂无符合条件的会话"
          />
        </div>
      </a-card>
    </a-layout-sider>

    <a-layout-content class="workspace-main">
      <a-card class="conversation-card" :bordered="false">
        <template #title>
          <div class="agent-current-session">
            <span class="agent-current-session-label">当前会话</span>
            <strong class="conversation-heading">{{
              currentSummary?.title ?? '请选择一条咨询'
            }}</strong>
            <span v-if="currentSummary" class="agent-current-session-meta">
              {{
                timeText(
                  currentSummary.last_message_at ?? currentSummary.created_at,
                )
              }}
            </span>
          </div>
        </template>
        <template #extra>
          <a-tag v-if="sopAssistance?.needs_manual_review" color="orange"
            >待人工核对</a-tag
          >
          <a-tag
            v-else-if="currentSummary"
            size="small"
            :color="
              currentSummary.status === 'ACTIVE'
                ? 'arcoblue'
                : currentSummary.status === 'WRAP_UP'
                  ? 'orange'
                  : 'gray'
            "
            >{{
              statusText[currentSummary.status] ?? currentSummary.status
            }}</a-tag
          >
        </template>

        <div
          v-if="conversation"
          class="conversation-live agent-conversation-live"
          :class="{ 'has-sop-review': sopAssistance?.needs_manual_review }"
        >
          <div
            v-if="sopAssistance?.needs_manual_review"
            class="sop-review-banner"
            role="status"
          >
            <span
              >{{ sopAssistance.name }}已暂停：{{ sopAssistance.reason }}</span
            >
            <a-button
              type="text"
              size="small"
              class="sop-review-action"
              :aria-expanded="processPanelVisible"
              aria-controls="agent-process-panel"
              @click="openProcessPanel"
              >立即处理 <IconRight
            /></a-button>
          </div>
          <div class="conversation-scroll-region">
            <div
              ref="messageList"
              class="conversation-message-list"
              aria-live="polite"
              @scroll="updateMessagePosition"
            >
              <div v-if="!messages.length" class="conversation-empty">
                <h2>暂无消息</h2>
                <p>客户发送消息后，会话内容会显示在这里。</p>
              </div>
              <div
                v-for="message in messages"
                :id="`agent-message-${message.message_id}`"
                :key="message.message_id"
                class="conversation-message"
                :class="{ 'from-customer': message.role === 'CUSTOMER' }"
              >
                <span class="message-sender">{{
                  message.role === 'CUSTOMER'
                    ? '客户'
                    : message.role === 'AGENT'
                      ? '人工客服'
                      : '客户服务'
                }}</span>
                <div class="message-bubble">
                  <StreamingText
                    :text="message.content"
                    :animate="animatedMessageIds.includes(message.message_id)"
                  />
                </div>
                <time>{{ timeText(message.created_at) }}</time>
              </div>
            </div>
            <button
              v-if="showScrollToBottom"
              class="scroll-to-bottom"
              type="button"
              aria-label="回到最新消息"
              @click="scrollToLatest"
            >
              <IconDown />
            </button>
            <div
              v-if="customerQuestions.length"
              class="question-navigator"
              aria-label="客户问题导航"
            >
              <button
                v-for="(question, index) in customerQuestions"
                :key="question.message_id"
                class="question-navigator-marker"
                :class="{ 'is-active': index === activeQuestionIndex }"
                type="button"
                :aria-label="`定位到问题 ${index + 1}：${questionPreview(question.content)}`"
                @click="locateQuestion(question.message_id)"
              >
                <span /><em>{{ questionPreview(question.content) }}</em>
              </button>
            </div>
          </div>
          <div class="agent-conversation-footer">
            <span>这里展示已经保存的完整会话消息。</span>
          </div>
        </div>
        <div v-else class="conversation-stage">
          <a-result
            status="info"
            title="请选择一条咨询"
            subtitle="选择客户后，可从右上角查看该客户的最近咨询。"
          />
        </div>
      </a-card>
    </a-layout-content>

    <a-layout-sider :width="376" class="workspace-sider details-sider">
      <a-card class="fill-card inspector-card" :bordered="false">
        <section
          v-if="processPanelVisible && sopAssistance"
          id="agent-process-panel"
          aria-label="流程处理"
        >
          <div class="sop-process-header">
            <a-button type="text" size="small" @click="closeProcessPanel">
              <template #icon><IconLeft /></template>
              返回
            </a-button>
            <strong>流程处理</strong>
          </div>
          <div class="sop-assistance-panel">
            <h3>{{ sopAssistance.name }}</h3>
            <a-tag
              :color="
                sopAssistance.needs_manual_review && !sopAssistance.processing
                  ? 'orange'
                  : 'arcoblue'
              "
              >{{
                sopAssistance.processing
                  ? '处理中'
                  : sopAssistance.needs_manual_review
                    ? '待人工核对'
                    : sopAssistance.status === 'COMPLETED'
                      ? '已完成'
                      : sopAssistance.status === 'CANCELLED'
                        ? '已取消'
                        : sopAssistance.status === 'SUSPENDED'
                          ? '已暂停'
                          : '自动收集资料'
              }}</a-tag
            >
            <p>{{ sopAssistance.reason }}</p>
            <a-alert v-if="sopAssistance.processing" type="info"
              >处理已受理，正在核对结果，请勿重复办理。</a-alert
            >
            <dl>
              <template
                v-for="(value, label) in sopAssistance.fields"
                :key="label"
                ><dt>{{ label }}</dt>
                <dd>{{ value }}</dd></template
              >
            </dl>
            <template v-if="sopAssistance.needs_manual_review">
              <p class="sop-assistance-guidance">
                {{ sopAssistance.guidance }}
              </p>
              <a-textarea
                v-model="assistanceDraft"
                placeholder="输入核对意见，发送给客户"
                :max-length="1000"
                :auto-size="{ minRows: 4, maxRows: 8 }"
                :disabled="
                  sopAssistance.processing || conversation?.status !== 'ACTIVE'
                "
                @keydown="submitAssistance"
              />
              <a-button
                type="primary"
                :loading="sendingAssistance"
                :disabled="
                  !assistanceDraft.trim() ||
                  sopAssistance.processing ||
                  conversation?.status !== 'ACTIVE'
                "
                @click="submitAssistance()"
                >发送处理意见</a-button
              >
              <div class="sop-review-controls">
                <a-button
                  v-if="sopAssistance.can_retry"
                  type="primary"
                  :loading="reviewingSop"
                  :disabled="sopAssistance.processing"
                  @click="submitReview('RETRY')"
                  >查询原操作结果</a-button
                >
                <template v-else>
                  <a-alert v-if="sopAssistance.approval_expired" type="warning">
                    本次审批已过期。请先重新核对办理资料，再勾选核验并批准继续；发送文字意见不会自动办理。
                  </a-alert>
                  <a-button
                    v-if="
                      sopAssistance.can_recheck && !sopAssistance.can_approve
                    "
                    type="primary"
                    :loading="reviewingSop"
                    :disabled="sopAssistance.processing"
                    @click="submitReview('RECHECK')"
                    >重新核对办理资料</a-button
                  >
                  <a-checkbox
                    v-if="sopAssistance.can_approve"
                    v-model="identityVerified"
                    :disabled="reviewingSop || sopAssistance.processing"
                    >已通过受保护渠道核验身份和办理条件</a-checkbox
                  >
                  <a-popconfirm
                    v-if="sopAssistance.can_approve"
                    content="确认批准并继续执行此业务操作？这不是发送聊天意见。"
                    @ok="submitReview('APPROVE')"
                  >
                    <a-button
                      type="primary"
                      :loading="reviewingSop"
                      :disabled="!identityVerified || sopAssistance.processing"
                      >批准并继续办理</a-button
                    >
                  </a-popconfirm>
                  <a-button
                    :disabled="reviewingSop || sopAssistance.processing"
                    @click="correctionVisible = !correctionVisible"
                    >{{
                      correctionVisible ? '收起更正资料' : '更正资料'
                    }}</a-button
                  >
                  <div v-if="correctionVisible" class="sop-correction-fields">
                    <div
                      v-for="field in sopAssistance.editable_fields"
                      :key="field.name"
                      class="sop-correction-field"
                    >
                      <span :id="`sop-correction-${field.name}-label`">{{
                        field.label
                      }}</span>
                      <span
                        v-if="Object.keys(field.choices ?? {}).length"
                        class="sop-correction-select-shell"
                      >
                        <select
                          v-model="correctedFields[field.name]"
                          class="sop-correction-select"
                          :disabled="reviewingSop"
                          :aria-labelledby="`sop-correction-${field.name}-label`"
                        >
                          <option
                            v-for="(label, value) in field.choices"
                            :key="value"
                            :value="value"
                          >
                            {{ label }}
                          </option>
                        </select>
                        <IconDown aria-hidden="true" />
                      </span>
                      <a-input
                        v-else
                        v-model="correctedFields[field.name]"
                        :disabled="reviewingSop"
                        :max-length="64"
                        :aria-labelledby="`sop-correction-${field.name}-label`"
                      />
                    </div>
                    <a-button
                      type="primary"
                      :loading="reviewingSop"
                      :disabled="
                        !Object.keys(changedFields).length ||
                        !assistanceDraft.trim() ||
                        sopAssistance.processing
                      "
                      @click="submitReview('CORRECT')"
                      >退回更正，要求客户重新确认</a-button
                    >
                  </div>
                  <a-popconfirm
                    content="确认拒绝本次办理？不会执行业务操作。"
                    @ok="submitReview('REJECT')"
                  >
                    <a-button
                      status="danger"
                      :disabled="
                        reviewingSop ||
                        !assistanceDraft.trim() ||
                        sopAssistance.processing
                      "
                      >拒绝本次办理</a-button
                    >
                  </a-popconfirm>
                  <small>更正或拒绝前，请在上方填写处理说明。</small>
                </template>
              </div>
            </template>
          </div>
        </section>
        <a-tabs v-else v-model:active-key="activePanel" :animation="false">
          <a-tab-pane key="customer" title="客户资料">
            <CustomerDetails
              :context="context"
              :loading="contextLoading"
              :agent="true"
            />
          </a-tab-pane>
          <a-tab-pane key="knowledge" title="业务知识">
            <a-empty description="当前暂无可查看的业务知识" />
          </a-tab-pane>
          <a-tab-pane key="assistant" title="辅助建议">
            <div v-if="recommendations.length" class="recommendation-list">
              <a-card
                v-for="item in recommendations"
                :key="item.recommendation_id"
                size="small"
                class="recommendation-card"
                @mouseenter="
                  ['RELEASED', 'SENT'].includes(reviewStatus(item)) &&
                  $emit(
                    'recommendationAction',
                    item.recommendation_id,
                    'VIEWED',
                  )
                "
              >
                <div class="recommendation-meta">
                  <a-tag color="arcoblue">{{ item.response_path }}</a-tag>
                  <a-tag v-if="reviewStatus(item) === 'DRAFT'" color="orange"
                    >待审核</a-tag
                  >
                  <a-tag v-else-if="reviewStatus(item) === 'SENT'" color="green"
                    >已发送</a-tag
                  >
                  <a-tag
                    v-else-if="reviewStatus(item) === 'REJECTED'"
                    color="gray"
                    >已拒绝</a-tag
                  >
                  <a-tag
                    v-else-if="reviewStatus(item) === 'EXPIRED'"
                    color="gray"
                    >已过期</a-tag
                  >
                  <span>{{
                    item.intent_decision.intent_id ?? '未识别意图'
                  }}</span>
                </div>
                <p class="recommendation-answer">{{ item.answer }}</p>
                <div
                  v-if="item.answer_sources.length"
                  class="recommendation-source"
                >
                  来源：{{
                    item.answer_sources[0]?.title ??
                    item.answer_sources[0]?.source_id
                  }}
                  · {{ item.answer_sources[0]?.version }}
                </div>
                <p
                  v-if="reviewStatus(item) === 'DRAFT'"
                  class="recommendation-review-note"
                >
                  草稿尚未发送给客户，等待坐席审核。
                  {{ reviewDeadlineText(item.review_expires_at) }}
                </p>
                <div
                  v-if="reviewStatus(item) === 'DRAFT'"
                  class="rag-review-form"
                >
                  <a-textarea
                    v-model="ragEditedAnswers[item.recommendation_id]"
                    :auto-size="{ minRows: 3, maxRows: 8 }"
                    :max-length="1800"
                    show-word-limit
                    placeholder="可在此修改完整答复"
                  />
                  <a-textarea
                    v-model="ragReviewNotes[item.recommendation_id]"
                    :auto-size="{ minRows: 2, maxRows: 4 }"
                    :max-length="1000"
                    placeholder="编辑或拒绝时必须填写处理原因"
                  />
                  <div class="recommendation-actions">
                    <a-popconfirm
                      content="确认采用原草稿并发送给客户？"
                      @ok="submitRagReview(item, 'APPROVE')"
                    >
                      <a-button
                        type="primary"
                        size="small"
                        :loading="reviewingRag"
                      >
                        采用并发送
                      </a-button>
                    </a-popconfirm>
                    <a-button
                      size="small"
                      :loading="reviewingRag"
                      :disabled="
                        !ragReviewNotes[item.recommendation_id]?.trim() ||
                        !ragEditedAnswers[item.recommendation_id]?.trim()
                      "
                      @click="submitRagReview(item, 'EDIT')"
                    >
                      编辑后发送
                    </a-button>
                    <a-popconfirm
                      content="确认拒绝该草稿并向客户发送安全提示？"
                      @ok="submitRagReview(item, 'REJECT')"
                    >
                      <a-button
                        status="danger"
                        size="small"
                        :loading="reviewingRag"
                        :disabled="
                          !ragReviewNotes[item.recommendation_id]?.trim()
                        "
                      >
                        拒绝草稿
                      </a-button>
                    </a-popconfirm>
                  </div>
                </div>
                <div v-else-if="reviewStatus(item) === 'EXPIRED'">
                  <p class="recommendation-review-note">
                    原草稿已超过审核时限，不能继续发送；请重新检索并生成草稿。
                  </p>
                  <div class="rag-review-form">
                    <a-textarea
                      v-model="ragReviewNotes[item.recommendation_id]"
                      :auto-size="{ minRows: 2, maxRows: 4 }"
                      :max-length="1000"
                      placeholder="请说明重新生成原因"
                    />
                    <div class="recommendation-actions">
                      <a-button
                        type="primary"
                        size="small"
                        :loading="reviewingRag"
                        :disabled="
                          !ragReviewNotes[item.recommendation_id]?.trim()
                        "
                        @click="submitRagReview(item, 'REGENERATE')"
                      >
                        重新检索并生成
                      </a-button>
                    </div>
                  </div>
                </div>
                <p
                  v-else-if="item.review_status === 'REJECTED'"
                  class="recommendation-review-note"
                >
                  草稿未通过坐席审核，原文仅保留用于审计。
                </p>
                <div v-else class="recommendation-actions">
                  <a-button size="small" @click="copyRecommendation(item)"
                    >复制</a-button
                  >
                  <a-button
                    size="small"
                    @click="
                      $emit(
                        'recommendationFeedback',
                        item.recommendation_id,
                        true,
                      )
                    "
                  >
                    有帮助
                  </a-button>
                  <a-button
                    size="small"
                    @click="
                      $emit(
                        'recommendationFeedback',
                        item.recommendation_id,
                        false,
                      )
                    "
                  >
                    无帮助
                  </a-button>
                </div>
              </a-card>
            </div>
            <a-empty v-else description="当前会话暂无辅助建议" />

            <a-divider />
            <div class="wrap-up-panel">
              <div class="wrap-up-heading">
                <strong>服务小结</strong>
                <a-tag v-if="wrapUp">
                  {{ wrapUp.status === 'CONFIRMED' ? '已确认' : '待确认' }}
                </a-tag>
              </div>
              <template v-if="wrapUp">
                <p>{{ wrapUp.topic_summary }}</p>
                <a-button
                  v-if="wrapUp.status !== 'CONFIRMED'"
                  type="primary"
                  size="small"
                  @click="$emit('confirmWrapUp')"
                >
                  确认并结束服务
                </a-button>
              </template>
              <a-button
                v-else
                size="small"
                :disabled="!conversation || !messages.length"
                @click="$emit('createWrapUp')"
              >
                生成小结草稿
              </a-button>
            </div>
          </a-tab-pane>
        </a-tabs>
      </a-card>
    </a-layout-sider>
  </a-layout>
</template>
