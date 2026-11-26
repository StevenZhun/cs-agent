<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import AgentWorkspace from './AgentWorkspace.vue'
import CustomerWorkspace from './CustomerWorkspace.vue'
import {
  clearSession,
  finishConversation,
  confirmWrapUp,
  createWrapUp,
  createConversation,
  getContext,
  getConversation,
  getCustomerTurn,
  getConversationTokenUsage,
  getModelOptions,
  getSopAssistance,
  isAuthenticationError,
  sendSopAssistanceReply,
  processSopReview,
  reviewRagDraft,
  listCustomers,
  listCustomerWorkloads,
  deleteConversation,
  listConversations,
  listMessages,
  listRecommendations,
  login,
  readSession,
  recordRecommendationInteraction,
  renameConversationTitle,
  saveSession,
  sendCustomerMessage,
  setConversationPinned,
  submitRecommendationFeedback,
  streamAgentEvents,
  streamConversationEvents,
  type Conversation,
  type ConversationSummary,
  type ConversationWrapUp,
  type ConversationMessage,
  type CustomerServiceContext,
  type CustomerProfile,
  type CustomerWorkload,
  type Principal,
  type Recommendation,
  type RagReview,
  type TurnTokenUsage,
  type ModelSelection,
  type ModelSelectionCatalog,
  type SopAssistance,
  type SopReview,
} from './api'

const principal = ref<Principal | null>(readSession()?.principal ?? null)
const username = ref('')
const password = ref('')
const busy = ref(false)
const contextLoading = ref(false)
const error = ref('')
const realtimeNotice = ref('')
const customers = ref<CustomerProfile[]>([])
const customerWorkloads = ref<CustomerWorkload[]>([])
const selectedCustomerId = ref('')
const context = ref<CustomerServiceContext | null>(null)
const conversation = ref<Conversation | null>(null)
const conversations = ref<ConversationSummary[]>([])
const messages = ref<ConversationMessage[]>([])
const recommendations = ref<Recommendation[]>([])
const wrapUp = ref<ConversationWrapUp | null>(null)
const sopAssistance = ref<SopAssistance | null>(null)
const sendingAssistance = ref(false)
const reviewingSop = ref(false)
const reviewingRag = ref(false)
const reviewSubmissions = new Map<
  string,
  {
    payload: SopReview
    key: string
  }
>()
const ragReviewSubmissions = new Map<
  string,
  {
    payload: RagReview
    key: string
  }
>()
let assistanceSubmission: {
  content: string
  executionId: string
  turnId: string
  key: string
} | null = null
let businessRefreshNeeded = false
const sending = ref(false)
const closingConversation = ref(false)
const pendingTurnId = ref('')
const turnTokenUsage = ref<TurnTokenUsage | null>(null)
const modelOptions = ref<ModelSelectionCatalog | null>(null)
const pendingSubmission = ref<{
  message_id: string
  content: string
  sent_at: string
  key: string
  conversation_model_profile: ModelSelection['conversation_model_profile']
  decision_mode: ModelSelection['decision_mode']
} | null>(null)
const lastRequestKey = ref('')
let contextRequest = 0
let pollTimer: ReturnType<typeof setTimeout> | undefined
let pollAttempts = 0
let eventController: AbortController | undefined
let eventRetryTimer: ReturnType<typeof setTimeout> | undefined
let eventRetryAttempts = 0
let eventCursor = 0
const eventsConnected = ref(false)
const pendingCustomerIds = ref<string[]>([])
const pendingConversationIds = ref<string[]>([])
let agentEventController: AbortController | undefined
let agentEventRetryTimer: ReturnType<typeof setTimeout> | undefined
let agentEventRetryAttempts = 0
let agentEventCursor = 0
let eventRefreshTimer: ReturnType<typeof setTimeout> | undefined
let agentRefreshTimer: ReturnType<typeof setTimeout> | undefined
let eventRefreshAgain = false
let agentRefreshAgain = false
let agentCurrentRefreshNeeded = false

function stopEvents() {
  if (eventRefreshTimer) clearTimeout(eventRefreshTimer)
  eventRefreshTimer = undefined
  eventRefreshAgain = false
  eventController?.abort()
  eventController = undefined
  if (eventRetryTimer) clearTimeout(eventRetryTimer)
  eventRetryTimer = undefined
  eventsConnected.value = false
  eventRetryAttempts = 0
  eventCursor = 0
}

function stopAgentEvents() {
  if (agentRefreshTimer) clearTimeout(agentRefreshTimer)
  agentRefreshTimer = undefined
  agentRefreshAgain = false
  agentCurrentRefreshNeeded = false
  agentEventController?.abort()
  agentEventController = undefined
  if (agentEventRetryTimer) clearTimeout(agentEventRetryTimer)
  agentEventRetryTimer = undefined
  agentEventRetryAttempts = 0
  agentEventCursor = 0
}

function reconnectDelay(attempt: number): number {
  return Math.min(60_000, 2_000 * 2 ** attempt)
}

function expireRealtimeSession(): void {
  signOut()
  error.value = '登录状态已过期，请重新登录'
}

function scheduleAgentEventReconnect(): void {
  const delay = reconnectDelay(agentEventRetryAttempts)
  agentEventRetryAttempts += 1
  realtimeNotice.value = `实时连接已断开，将在 ${Math.ceil(delay / 1000)} 秒后重连。`
  agentEventRetryTimer = setTimeout(connectAgentEvents, delay)
}

function scheduleConversationEventReconnect(conversationId: string): void {
  const delay = reconnectDelay(eventRetryAttempts)
  eventRetryAttempts += 1
  realtimeNotice.value = `实时连接已断开，将在 ${Math.ceil(delay / 1000)} 秒后重连。`
  eventRetryTimer = setTimeout(() => connectEvents(conversationId), delay)
}

/** 将一批事件合成一次页面读取，避免历史补发逐条触发多个接口而撞上限流。 */
function scheduleEventRefresh(agent: boolean, current = true): void {
  if (agent && current) agentCurrentRefreshNeeded = true
  if (agent ? agentRefreshTimer : eventRefreshTimer) {
    if (agent) agentRefreshAgain = true
    else eventRefreshAgain = true
    return
  }
  const selectedId = conversation.value?.conversation_id
  const customerId = selectedCustomerId.value
  const timer = setTimeout(async () => {
    try {
      const refreshCurrent = !agent || agentCurrentRefreshNeeded
      if (agent) agentCurrentRefreshNeeded = false
      // 所有授权客户的计数都刷新，不只刷新当前选中客户；一批事件只请求一次。
      if (agent) customerWorkloads.value = await listCustomerWorkloads()
      if (!refreshCurrent) return
      if (selectedId && selectedId === conversation.value?.conversation_id) {
        await refreshMessages()
        if (agent) await refreshRecommendations()
        else {
          conversation.value = await getConversation(selectedId)
          if (pendingTurnId.value) await refreshTurn()
        }
      }
      if (customerId && customerId === selectedCustomerId.value) {
        // 只有已提交的业务动作才刷新服务事实，普通消息不增加业务查询。
        if (businessRefreshNeeded) {
          businessRefreshNeeded = false
          const latestContext = await getContext(customerId)
          if (customerId === selectedCustomerId.value)
            context.value = latestContext
        }
        const recent = await listConversations(customerId)
        if (customerId === selectedCustomerId.value)
          conversations.value = recent
        if (agent && selectedId === conversation.value?.conversation_id) {
          clearPending(pendingConversationIds, selectedId ?? '')
          clearCustomerNoticeIfRead(customerId)
        }
      }
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '更新会话失败'
    } finally {
      if (agent) agentRefreshTimer = undefined
      else eventRefreshTimer = undefined
      const again = agent ? agentRefreshAgain : eventRefreshAgain
      if (agent) agentRefreshAgain = false
      else eventRefreshAgain = false
      if (again && principal.value)
        scheduleEventRefresh(agent, agent ? agentCurrentRefreshNeeded : true)
    }
  }, 150)
  if (agent) agentRefreshTimer = timer
  else eventRefreshTimer = timer
}

function markPending(list: typeof pendingCustomerIds, value: string) {
  if (!list.value.includes(value)) list.value = [...list.value, value]
}

function clearPending(list: typeof pendingCustomerIds, value: string) {
  list.value = list.value.filter((item) => item !== value)
}

/** 只有该客户的所有待查看会话都被点开后，才移除客户列表蓝点。 */
function clearCustomerNoticeIfRead(
  customerId: string,
  items = conversations.value,
) {
  const hasPendingConversation = items.some((item) =>
    pendingConversationIds.value.includes(item.conversation_id),
  )
  if (!hasPendingConversation) clearPending(pendingCustomerIds, customerId)
}

function connectAgentEvents() {
  if (!isAgent.value) return
  agentEventController?.abort()
  const controller = new AbortController()
  agentEventController = controller
  void streamAgentEvents(
    agentEventCursor,
    controller.signal,
    () => {
      agentEventRetryAttempts = 0
      realtimeNotice.value = ''
    },
    async (event) => {
      if (controller.signal.aborted || event.sequence <= agentEventCursor)
        return
      agentEventCursor = event.sequence
      const customerId =
        typeof event.payload.customer_id === 'string'
          ? event.payload.customer_id
          : ''
      const isNewCustomerMessage =
        event.event_type === 'MESSAGE_CREATED' &&
        event.payload.role === 'CUSTOMER'
      if (customerId && isNewCustomerMessage)
        markPending(pendingCustomerIds, customerId)
      if (isNewCustomerMessage)
        markPending(pendingConversationIds, event.conversation_id)
      if (event.conversation_id === conversation.value?.conversation_id) {
        if (event.payload.business_changed === true)
          businessRefreshNeeded = true
      }
      scheduleEventRefresh(true, customerId === selectedCustomerId.value)
    },
  )
    .then(() => {
      if (!controller.signal.aborted) scheduleAgentEventReconnect()
    })
    .catch((cause) => {
      if (controller.signal.aborted) return
      if (isAuthenticationError(cause)) {
        expireRealtimeSession()
        return
      }
      scheduleAgentEventReconnect()
    })
}

function connectEvents(conversationId: string) {
  eventController?.abort()
  const controller = new AbortController()
  eventController = controller
  void streamConversationEvents(
    conversationId,
    eventCursor,
    controller.signal,
    () => {
      if (!controller.signal.aborted) {
        eventsConnected.value = true
        eventRetryAttempts = 0
        realtimeNotice.value = ''
        if (pollTimer) clearTimeout(pollTimer)
      }
    },
    async (event) => {
      if (
        controller.signal.aborted ||
        conversation.value?.conversation_id !== conversationId
      )
        return
      if (event.sequence <= eventCursor) return
      eventCursor = event.sequence
      if (event.event_type === 'MESSAGE_CREATED') {
        if (event.payload.business_changed === true)
          businessRefreshNeeded = true
        scheduleEventRefresh(false)
      } else if (event.event_type === 'CONVERSATION_UPDATED') {
        scheduleEventRefresh(false)
      }
    },
  )
    .then(() => {
      if (
        controller.signal.aborted ||
        conversation.value?.conversation_id !== conversationId
      )
        return
      scheduleTurnPoll()
      scheduleConversationEventReconnect(conversationId)
    })
    .catch((cause) => {
      if (controller.signal.aborted) return
      eventsConnected.value = false
      if (isAuthenticationError(cause)) {
        expireRealtimeSession()
        return
      }
      scheduleTurnPoll()
      scheduleConversationEventReconnect(conversationId)
    })
    .finally(() => {
      if (
        controller.signal.aborted ||
        conversation.value?.conversation_id !== conversationId
      )
        return
      eventsConnected.value = false
    })
}

function resetMessages() {
  stopEvents()
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = undefined
  pollAttempts = 0
  messages.value = []
  recommendations.value = []
  wrapUp.value = null
  sopAssistance.value = null
  assistanceSubmission = null
  pendingTurnId.value = ''
  pendingSubmission.value = null
  turnTokenUsage.value = null
}

const isAgent = computed(() =>
  principal.value?.roles.some((role) => role === 'AGENT' || role === 'ADMIN'),
)
const selectedCustomer = computed(() =>
  customers.value.find((item) => item.customer_id === selectedCustomerId.value),
)

function signOut() {
  reviewSubmissions.clear()
  businessRefreshNeeded = false
  contextRequest += 1
  clearSession()
  principal.value = null
  username.value = ''
  password.value = ''
  customers.value = []
  customerWorkloads.value = []
  selectedCustomerId.value = ''
  context.value = null
  conversation.value = null
  conversations.value = []
  resetMessages()
  stopAgentEvents()
  pendingCustomerIds.value = []
  pendingConversationIds.value = []
  lastRequestKey.value = ''
  error.value = ''
  realtimeNotice.value = ''
}

async function refreshCustomers() {
  busy.value = true
  error.value = ''
  try {
    customers.value = await listCustomers()
    if (isAgent.value) {
      customerWorkloads.value = await listCustomerWorkloads()
      connectAgentEvents()
    }
    if (
      !customers.value.some(
        (item) => item.customer_id === selectedCustomerId.value,
      )
    ) {
      selectedCustomerId.value = customers.value[0]?.customer_id ?? ''
    }
    await loadContext()
  } catch (cause) {
    if (!readSession()) signOut()
    error.value = cause instanceof Error ? cause.message : '加载资料失败'
  } finally {
    busy.value = false
  }
}

async function loadContext() {
  const request = ++contextRequest
  context.value = null
  conversation.value = null
  conversations.value = []
  resetMessages()
  lastRequestKey.value = ''
  const customerId = selectedCustomerId.value
  if (!customerId) {
    contextLoading.value = false
    return
  }
  contextLoading.value = true
  try {
    const [result, recent] = await Promise.all([
      getContext(customerId),
      listConversations(customerId),
    ])
    if (request === contextRequest) {
      context.value = result
      conversations.value = recent
      // 客户列表蓝点只代表该客户仍有未查看会话；切入后若没有待处理会话就清除。
      clearCustomerNoticeIfRead(customerId, recent)
      // 登录后优先打开仍可继续沟通的会话，避免首页默认停在已结束记录。
      const initialConversation =
        recent.find((item) => item.status === 'ACTIVE') ?? recent[0]
      if (initialConversation) {
        await selectConversation(initialConversation.conversation_id)
      }
    }
  } catch (cause) {
    if (request === contextRequest) {
      error.value = cause instanceof Error ? cause.message : '加载业务资料失败'
    }
  } finally {
    if (request === contextRequest) contextLoading.value = false
  }
}

function selectCustomer(customerId: string) {
  if (selectedCustomerId.value === customerId) return
  selectedCustomerId.value = customerId
  void loadContext()
}

async function signIn() {
  if (!username.value.trim() || !password.value) {
    error.value = '请输入账号和密码'
    return
  }
  busy.value = true
  error.value = ''
  try {
    const session = await login(username.value.trim(), password.value)
    saveSession(session)
    principal.value = session.principal
    password.value = ''
    modelOptions.value = await getModelOptions()
    await refreshCustomers()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '登录失败'
  } finally {
    busy.value = false
  }
}

async function startConversation() {
  if (!selectedCustomerId.value) return
  busy.value = true
  error.value = ''
  try {
    if (!lastRequestKey.value) lastRequestKey.value = crypto.randomUUID()
    conversation.value = await createConversation(
      selectedCustomerId.value,
      lastRequestKey.value,
    )
    messages.value = await listMessages(conversation.value.conversation_id)
    conversations.value = await listConversations(selectedCustomerId.value)
    if (!isAgent.value) connectEvents(conversation.value.conversation_id)
    lastRequestKey.value = ''
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '发起咨询失败'
  } finally {
    busy.value = false
  }
}

async function selectConversation(conversationId: string) {
  busy.value = true
  error.value = ''
  resetMessages()
  clearPending(pendingConversationIds, conversationId)
  try {
    conversation.value = await getConversation(conversationId)
    messages.value = await listMessages(conversationId)
    // 切换历史会话时读取该会话累计用量，而不是把圆环留在上一条 Turn。
    turnTokenUsage.value = await getConversationTokenUsage(conversationId)
    clearCustomerNoticeIfRead(selectedCustomerId.value)
    if (isAgent.value) await refreshRecommendations()
    // 坐席已有覆盖全部客户的事件连接，不再给当前会话重复建立第二条连接。
    if (!isAgent.value) connectEvents(conversationId)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '加载咨询失败'
  } finally {
    busy.value = false
  }
}

async function refreshRecommendations() {
  if (!conversation.value || !isAgent.value) return
  const id = conversation.value.conversation_id
  const [items, progress] = await Promise.all([
    listRecommendations(id),
    getSopAssistance(id),
  ])
  if (conversation.value?.conversation_id !== id) return
  recommendations.value = items
  sopAssistance.value = progress
  // 服务端明确完成或改变审批后，旧动作不再需要重发；不因网络断开自行判定失败。
  const submitted = reviewSubmissions.get(id)
  if (
    submitted &&
    progress &&
    !progress.processing &&
    (progress.approval_id !== submitted.payload.approval_id ||
      progress.approval_status === 'APPROVED' ||
      progress.approval_status === 'REJECTED' ||
      !progress.needs_manual_review)
  )
    reviewSubmissions.delete(id)
}

/** 同一人工意见重试沿用操作编号，避免网络超时后产生两条重复回复。 */
async function sendAssistance(content: string): Promise<void> {
  if (!conversation.value || !sopAssistance.value || sendingAssistance.value)
    return
  const id = conversation.value.conversation_id
  const view = sopAssistance.value
  if (assistanceSubmission && assistanceSubmission.content !== content) {
    error.value = '上一条处理意见的发送状态尚未确认，请先重试原内容。'
    return
  }
  assistanceSubmission ??= {
    content,
    executionId: view.sop_execution_id,
    turnId: view.turn_id,
    key: crypto.randomUUID(),
  }
  const saved = assistanceSubmission
  sendingAssistance.value = true
  error.value = ''
  try {
    await sendSopAssistanceReply(
      id,
      {
        content: saved.content,
        sop_execution_id: saved.executionId,
        turn_id: saved.turnId,
      },
      saved.key,
    )
    assistanceSubmission = null
    if (conversation.value?.conversation_id === id) await refreshMessages()
  } catch (cause) {
    const status = (cause as { status?: number })?.status
    // 明确拒绝没有保存意见，清除旧请求；网络不明仍保留原编号供重试。
    if (status && status >= 400 && status < 500) {
      assistanceSubmission = null
      if (conversation.value?.conversation_id === id) {
        try {
          await refreshRecommendations()
        } catch {
          /* 刷新也失败时仍展示原发送错误，不把它隐藏成未处理异常。 */
        }
      }
    }
    error.value = cause instanceof Error ? cause.message : '发送处理意见失败'
  } finally {
    sendingAssistance.value = false
  }
}

/** 人工动作先落库再恢复图；网络异常保留原请求，重复点击不会创建第二次操作。 */
async function reviewSop(payload: SopReview): Promise<void> {
  if (!conversation.value || reviewingSop.value) return
  const id = conversation.value.conversation_id
  let reviewSubmission = reviewSubmissions.get(id)
  if (
    reviewSubmission &&
    JSON.stringify(reviewSubmission.payload) !== JSON.stringify(payload)
  ) {
    error.value = '上一项处理的响应尚未确认，请先重试原动作。'
    return
  }
  reviewSubmission ??= { payload, key: crypto.randomUUID() }
  reviewSubmissions.set(id, reviewSubmission)
  reviewingSop.value = true
  error.value = ''
  try {
    const result = await processSopReview(
      id,
      reviewSubmission.payload,
      reviewSubmission.key,
    )
    reviewSubmissions.delete(id)
    if (conversation.value?.conversation_id === id) {
      await refreshMessages()
      await refreshRecommendations()
      conversations.value = await listConversations(selectedCustomerId.value)
      if (result.status === 'RUNNING')
        error.value = '处理已受理，正在核对结果，请勿重复办理。'
      const customerId = selectedCustomerId.value
      const business = await getContext(customerId)
      if (selectedCustomerId.value === customerId) context.value = business
    }
  } catch (cause) {
    const status = (cause as { status?: number })?.status
    // 4xx 是明确未受理；5xx 或网络中断可能发生在提交之后，保留原重试键。
    if (status && status >= 400 && status < 500) reviewSubmissions.delete(id)
    if (status === 409 && conversation.value?.conversation_id === id) {
      try {
        await refreshRecommendations()
      } catch {
        /* 保留原状态冲突说明，用户仍可重新读取待办。 */
      }
    }
    error.value = cause instanceof Error ? cause.message : '处理流程失败'
  } finally {
    reviewingSop.value = false
  }
}

/** 草稿审核使用稳定幂等键；超时后只能重试原动作，不能改成另一种处理。 */
async function reviewRag(payload: RagReview): Promise<void> {
  if (!conversation.value || reviewingRag.value) return
  const id = conversation.value.conversation_id
  let submission = ragReviewSubmissions.get(id)
  if (
    submission &&
    JSON.stringify(submission.payload) !== JSON.stringify(payload)
  ) {
    error.value = '上一项草稿审核的结果尚未确认，请先重试原动作。'
    return
  }
  submission ??= { payload, key: crypto.randomUUID() }
  ragReviewSubmissions.set(id, submission)
  reviewingRag.value = true
  error.value = ''
  try {
    await reviewRagDraft(id, submission.payload, submission.key)
    ragReviewSubmissions.delete(id)
    if (conversation.value?.conversation_id === id) {
      await refreshMessages()
      await refreshRecommendations()
      conversations.value = await listConversations(selectedCustomerId.value)
      customerWorkloads.value = await listCustomerWorkloads()
    }
  } catch (cause) {
    const status = (cause as { status?: number })?.status
    if (status && status >= 400 && status < 500) ragReviewSubmissions.delete(id)
    if (status === 409 && conversation.value?.conversation_id === id) {
      try {
        await refreshRecommendations()
      } catch {
        /* 保留原状态冲突说明，坐席仍能看到当前权威草稿。 */
      }
    }
    error.value = cause instanceof Error ? cause.message : '审核知识草稿失败'
  } finally {
    reviewingRag.value = false
  }
}

async function recordRecommendation(
  recommendationId: string,
  action: 'VIEWED' | 'COPIED',
) {
  if (!conversation.value) return
  try {
    await recordRecommendationInteraction(
      conversation.value.conversation_id,
      recommendationId,
      action,
    )
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '记录推荐操作失败'
  }
}

async function rateRecommendation(recommendationId: string, helpful: boolean) {
  if (!conversation.value) return
  try {
    await submitRecommendationFeedback(
      conversation.value.conversation_id,
      recommendationId,
      helpful,
    )
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '提交评价失败'
  }
}

async function makeWrapUp() {
  if (!conversation.value) return
  try {
    wrapUp.value = await createWrapUp(conversation.value.conversation_id)
    conversation.value = await getConversation(
      conversation.value.conversation_id,
    )
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '生成小结失败'
  }
}

async function finishWrapUp() {
  if (!conversation.value || !wrapUp.value) return
  try {
    wrapUp.value = await confirmWrapUp(
      conversation.value.conversation_id,
      wrapUp.value.version,
    )
    conversation.value = await getConversation(
      conversation.value.conversation_id,
    )
    conversations.value = await listConversations(selectedCustomerId.value)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '确认小结失败'
  }
}

async function verifyConversation() {
  if (!conversation.value) return
  try {
    conversation.value = await getConversation(
      conversation.value.conversation_id,
    )
    await refreshMessages()
    if (pendingTurnId.value) await refreshTurn()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '更新咨询状态失败'
  }
}

async function refreshMessages() {
  if (!conversation.value) return
  const id = conversation.value.conversation_id
  const result = await listMessages(id)
  if (conversation.value?.conversation_id === id) messages.value = result
}

function scheduleTurnPoll() {
  if (!pendingTurnId.value || eventsConnected.value || pollAttempts >= 15)
    return
  if (pollTimer) clearTimeout(pollTimer)
  pollTimer = setTimeout(() => void refreshTurn(), 2000)
}

async function refreshTurn() {
  if (!conversation.value || !pendingTurnId.value) return
  const conversationId = conversation.value.conversation_id
  const turnId = pendingTurnId.value
  pollAttempts += 1
  try {
    const result = await getCustomerTurn(conversationId, turnId)
    if (conversation.value?.conversation_id !== conversationId) return
    if (result.status === 'COMPLETED' || result.status === 'INTERRUPTED') {
      turnTokenUsage.value = result.token_usage ?? null
      pendingTurnId.value = ''
      pendingSubmission.value = null
      await refreshMessages()
      return
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '更新咨询结果失败'
  }
  scheduleTurnPoll()
}

async function sendMessage(content: string, selection: ModelSelection) {
  if (!conversation.value || sending.value || pendingTurnId.value) return
  if (pendingSubmission.value && pendingSubmission.value.content !== content) {
    error.value = '上一条消息的发送状态尚未确认，请重试原消息。'
    return
  }
  if (!pendingSubmission.value) {
    pendingSubmission.value = {
      message_id: `msg_${crypto.randomUUID().replaceAll('-', '')}`,
      content,
      sent_at: new Date().toISOString(),
      key: crypto.randomUUID(),
      ...selection,
    }
  }
  const submission = pendingSubmission.value
  const conversationId = conversation.value.conversation_id
  sending.value = true
  error.value = ''
  try {
    const result = await sendCustomerMessage(
      conversationId,
      submission,
      submission.key,
    )
    if (conversation.value?.conversation_id !== conversationId) return
    await refreshMessages()
    turnTokenUsage.value = result.token_usage ?? null
    conversations.value = await listConversations(selectedCustomerId.value)
    if (result.status === 'COMPLETED' || result.status === 'INTERRUPTED') {
      pendingSubmission.value = null
      pendingTurnId.value = ''
    } else {
      pendingTurnId.value = result.turn_id
      pollAttempts = 0
      // SSE 事件可能在这里之前就已到达，立即读取一次结果可避免错过完成事件，
      // 否则事件连接已建立时不会启动轮询，Token 用量也会一直显示为暂无统计。
      await refreshTurn()
    }
  } catch (cause) {
    error.value =
      cause instanceof Error ? cause.message : '发送状态未确认，请重试。'
  } finally {
    sending.value = false
  }
}

/** 保存客户输入的新标题，并只替换最近咨询中的对应条目。 */
async function renameConversation(conversationId: string, title: string) {
  error.value = ''
  try {
    await renameConversationTitle(conversationId, title)
    conversations.value = await listConversations(selectedCustomerId.value)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '修改会话标题失败'
  }
}

/** 保存当前账户自己的置顶状态，并重新读取列表以采用后端排序。 */
async function toggleConversationPinned(
  conversationId: string,
  pinned: boolean,
) {
  error.value = ''
  try {
    await setConversationPinned(conversationId, pinned)
    conversations.value = await listConversations(selectedCustomerId.value)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '修改会话置顶失败'
  }
}

/** 客户确认删除后重新读列表；只移除自己的记录，坐席审计仍保留。 */
async function removeConversation(conversationId: string): Promise<void> {
  error.value = ''
  try {
    await deleteConversation(conversationId)
    conversations.value = await listConversations(selectedCustomerId.value)
    if (conversation.value?.conversation_id === conversationId) {
      resetMessages()
      conversation.value = null
      const next = conversations.value[0]
      if (next) await selectConversation(next.conversation_id)
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '删除咨询失败'
  }
}

/** 客户确认结束后刷新当前状态和最近咨询，页面不再允许继续发送。 */
async function closeCurrentConversation() {
  if (
    !conversation.value ||
    closingConversation.value ||
    sending.value ||
    pendingTurnId.value
  )
    return
  const conversationId = conversation.value.conversation_id
  closingConversation.value = true
  error.value = ''
  try {
    conversation.value = await finishConversation(conversationId)
    conversations.value = await listConversations(selectedCustomerId.value)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '结束咨询失败'
  } finally {
    closingConversation.value = false
  }
}

onMounted(() => {
  if (principal.value) {
    void getModelOptions().then((options) => {
      modelOptions.value = options
    })
    void refreshCustomers()
  }
})
onBeforeUnmount(() => {
  stopEvents()
  stopAgentEvents()
})
</script>

<template>
  <div v-if="!principal" class="login-screen">
    <header class="login-brand">
      <span class="brand-mark">A</span>
      <strong>客户服务</strong>
    </header>
    <a-card class="login-card" :bordered="false">
      <div class="login-heading">
        <a-avatar :size="48" class="login-avatar">A</a-avatar>
        <h1>欢迎回来</h1>
        <p>请使用您的账号登录。</p>
      </div>
      <form class="login-form" @submit.prevent="signIn">
        <label for="login-username">账号</label>
        <a-input
          id="login-username"
          v-model="username"
          size="large"
          autocomplete="username"
          placeholder="请输入账号"
        />
        <label for="login-password">密码</label>
        <a-input-password
          id="login-password"
          v-model="password"
          size="large"
          autocomplete="current-password"
          placeholder="请输入密码"
        />
        <a-alert v-if="error" type="error">{{ error }}</a-alert>
        <a-button
          html-type="submit"
          type="primary"
          size="large"
          long
          :loading="busy"
        >
          登录
        </a-button>
      </form>
      <a-divider />
      <p class="login-reminder">保护账户信息，请勿向他人透露密码或验证码。</p>
    </a-card>
  </div>

  <a-layout v-else class="app-shell">
    <a-layout-header class="app-header">
      <div class="brand-lockup">
        <span class="brand-mark">A</span>
        <strong>客户服务</strong>
        <a-divider v-if="isAgent" direction="vertical" />
        <span v-if="isAgent" class="header-context">坐席工作台</span>
      </div>
      <div class="account-actions">
        <div
          class="account-profile"
          :aria-label="`当前账户：${isAgent ? '坐席' : (selectedCustomer?.display_name ?? '客户')}`"
        >
          <a-avatar :size="30">{{
            isAgent
              ? '坐'
              : (selectedCustomer?.display_name.slice(0, 1) ?? '客')
          }}</a-avatar>
        </div>
        <a-button type="text" class="logout-button" @click="signOut">
          退出
        </a-button>
      </div>
    </a-layout-header>

    <a-alert
      v-if="realtimeNotice"
      type="warning"
      closable
      class="page-alert"
      @close="realtimeNotice = ''"
    >
      {{ realtimeNotice }}
    </a-alert>

    <a-alert
      v-if="error"
      type="error"
      closable
      class="page-alert"
      @close="error = ''"
    >
      {{ error }}
    </a-alert>

    <a-layout-content class="app-content">
      <AgentWorkspace
        v-if="isAgent"
        :customers="customers"
        :customer-workloads="customerWorkloads"
        :selected-customer-id="selectedCustomerId"
        :context="context"
        :context-loading="contextLoading"
        :busy="busy"
        :conversations="conversations"
        :conversation="conversation"
        :messages="messages"
        :recommendations="recommendations"
        :wrap-up="wrapUp"
        :sop-assistance="sopAssistance"
        :sending-assistance="sendingAssistance"
        :reviewing-sop="reviewingSop"
        :reviewing-rag="reviewingRag"
        @send-assistance="sendAssistance"
        @review-sop="reviewSop"
        @review-rag="reviewRag"
        :pending-customer-ids="pendingCustomerIds"
        :pending-conversation-ids="pendingConversationIds"
        @select="selectCustomer"
        @select-conversation="selectConversation"
        @recommendation-action="recordRecommendation"
        @recommendation-feedback="rateRecommendation"
        @create-wrap-up="makeWrapUp"
        @confirm-wrap-up="finishWrapUp"
      />
      <CustomerWorkspace
        v-else
        :customer="selectedCustomer"
        :context="context"
        :context-loading="contextLoading"
        :conversation="conversation"
        :conversations="conversations"
        :messages="messages"
        :sending="sending"
        :pending="Boolean(pendingTurnId)"
        :token-usage="turnTokenUsage"
        :model-options="modelOptions"
        :closing="closingConversation"
        :busy="busy"
        @start="startConversation"
        @select-conversation="selectConversation"
        @refresh="verifyConversation"
        @send="sendMessage"
        @close="closeCurrentConversation"
        @rename-conversation="renameConversation"
        @toggle-pinned="toggleConversationPinned"
        @delete-conversation="removeConversation"
        @refresh-turn="refreshTurn"
      />
    </a-layout-content>
  </a-layout>
</template>
