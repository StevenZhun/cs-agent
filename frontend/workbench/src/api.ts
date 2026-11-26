/** Types generated from the BFF's own OpenAPI contract, never copied by hand. */
import type { components } from './openapi'

type Schemas = components['schemas']
export type Principal = Schemas['AuthPrincipal']
export type LoginResponse = Schemas['LoginResponse']
export type CustomerProfile = Schemas['CustomerProfile']
export type CustomerServiceContext = Schemas['CustomerServiceContext']
export type Conversation = Schemas['CreateConversationResponse']
export type ConversationSummary = Schemas['ConversationSummary']
export type CustomerWorkload = Schemas['CustomerConversationWorkload']

/** 一次读取所有授权客户的非结束会话数与人工待办数。 */
export async function listCustomerWorkloads(): Promise<CustomerWorkload[]> {
  const response = await call<{ data: CustomerWorkload[] }>(
    '/agent/customer-workloads',
  )
  return response.data
}

/** 从客户自己的列表软删除已结束咨询，不物理删除历史或业务审计。 */
export async function deleteConversation(
  conversationId: string,
): Promise<void> {
  await call(`/conversations/${encodeURIComponent(conversationId)}`, {
    method: 'DELETE',
  })
}
export type ConversationMessage = Schemas['ConversationMessage']
export type CustomerTurnResult = Schemas['CustomerTurnResult']
export type TurnTokenUsage = Schemas['TurnTokenUsage']
export type ModelSelectionCatalog = Schemas['ModelSelectionCatalog']
export type ModelSelection = Pick<
  Schemas['SendMessageRequest'],
  'conversation_model_profile' | 'decision_mode'
>
export type ConversationEvent = Schemas['RealtimeEventEnvelope']
export type Recommendation = Schemas['RecommendationResult']
export type RagReview = Schemas['RagReviewRequest']
export type RagReviewResult = Schemas['RagReviewResult']
export type ConversationWrapUp = Schemas['ConversationWrapUp']
export type SopAssistance = Schemas['SopAssistanceView']
export type SopReview = Schemas['SopReviewRequest']

/** 发送独立的坐席处理动作；同一操作重试使用原幂等键。 */
export async function processSopReview(
  conversationId: string,
  payload: SopReview,
  key: string,
): Promise<CustomerTurnResult> {
  const response = await call<Schemas['ApiResponse_CustomerTurnResult_']>(
    `/conversations/${encodeURIComponent(conversationId)}/sop/review`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': key },
      body: JSON.stringify(payload),
    },
  )
  return response.data
}

/** 获取坐席有权访问的流程进度，包含真实暂停原因。 */
export async function getSopAssistance(
  conversationId: string,
): Promise<SopAssistance | null> {
  const result = await call<{ data: SopAssistance | null }>(
    `/conversations/${encodeURIComponent(conversationId)}/sop`,
  )
  return result.data
}

/** 发送人工处理意见，不把文字回复当作审批通过。 */
export async function sendSopAssistanceReply(
  conversationId: string,
  payload: Schemas['SopAssistanceReply'],
  key: string,
): Promise<void> {
  await call(
    `/conversations/${encodeURIComponent(conversationId)}/sop/replies`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': key },
      body: JSON.stringify(payload),
    },
  )
}
type CustomerListResponse =
  Schemas['ApiResponse_tuple_CustomerProfile__________']
type CustomerContextResponse = Schemas['ApiResponse_CustomerServiceContext_']
type ConversationResponse = Schemas['ApiResponse_CreateConversationResponse_']
type MessageListResponse =
  Schemas['ApiResponse_tuple_ConversationMessage__________']
type CustomerTurnResponse = Schemas['ApiResponse_CustomerTurnResult_']
type TokenUsageResponse =
  Schemas['ApiResponse_Union_TurnTokenUsage__NoneType__']
type ConversationListResponse =
  Schemas['ApiResponse_tuple_ConversationSummary__________']
type RecommendationListResponse =
  Schemas['ApiResponse_tuple_RecommendationResult__________']
type WrapUpResponse = Schemas['ApiResponse_ConversationWrapUp_']

const TOKEN_KEY = 'crs_access_token'
const PRINCIPAL_KEY = 'crs_principal'

export class ApiResponseError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message)
    this.name = 'ApiResponseError'
  }
}

export function isAuthenticationError(cause: unknown): boolean {
  return cause instanceof ApiResponseError && cause.status === 401
}

export function readSession(): { token: string; principal: Principal } | null {
  const token = sessionStorage.getItem(TOKEN_KEY)
  const encoded = sessionStorage.getItem(PRINCIPAL_KEY)
  if (!token || !encoded) return null
  try {
    return { token, principal: JSON.parse(encoded) as Principal }
  } catch {
    return null
  }
}

export function saveSession(session: LoginResponse): void {
  sessionStorage.setItem(TOKEN_KEY, session.access_token)
  sessionStorage.setItem(PRINCIPAL_KEY, JSON.stringify(session.principal))
}

export function clearSession(): void {
  sessionStorage.removeItem(TOKEN_KEY)
  sessionStorage.removeItem(PRINCIPAL_KEY)
}

async function call<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = readSession()?.token
  const response = await fetch(`/api/v2${path}`, {
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...options.headers,
    },
  })
  const payload: unknown = await response.json().catch(() => null)
  if (!response.ok) {
    if (response.status === 401 && path !== '/auth/login') clearSession()
    const message =
      payload &&
      typeof payload === 'object' &&
      'message' in payload &&
      typeof payload.message === 'string'
        ? payload.message
        : `请求失败 (${response.status})`
    throw new ApiResponseError(message, response.status)
  }
  return payload as T
}

export function login(
  username: string,
  password: string,
): Promise<LoginResponse> {
  return call('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export async function listCustomers(): Promise<CustomerProfile[]> {
  return (await call<CustomerListResponse>('/customers')).data
}

export async function getContext(
  customerId: string,
): Promise<CustomerServiceContext> {
  return (
    await call<CustomerContextResponse>(
      `/customers/${encodeURIComponent(customerId)}/context`,
    )
  ).data
}

export async function listConversations(
  customerId: string,
): Promise<ConversationSummary[]> {
  return [
    ...(
      await call<ConversationListResponse>(
        `/customers/${encodeURIComponent(customerId)}/conversations`,
      )
    ).data,
  ]
}

export async function createConversation(
  customerId: string,
  key: string,
): Promise<Conversation> {
  return (
    await call<ConversationResponse>('/conversations', {
      method: 'POST',
      headers: { 'Idempotency-Key': key },
      body: JSON.stringify({ customer_id: customerId, channel: 'WEB' }),
    })
  ).data
}

export async function getConversation(
  conversationId: string,
): Promise<Conversation> {
  return (
    await call<ConversationResponse>(
      `/conversations/${encodeURIComponent(conversationId)}`,
    )
  ).data
}

/** 保存会话标题，并返回可直接替换最近咨询列表项的新数据。 */
export async function renameConversationTitle(
  conversationId: string,
  title: string,
): Promise<ConversationSummary> {
  return (
    await call<Schemas['ApiResponse_ConversationSummary_']>(
      `/conversations/${encodeURIComponent(conversationId)}/title`,
      {
        method: 'PATCH',
        body: JSON.stringify({ title }),
      },
    )
  ).data
}

/** 保存当前登录账户自己的会话置顶状态。 */
export async function setConversationPinned(
  conversationId: string,
  pinned: boolean,
): Promise<ConversationSummary> {
  return (
    await call<Schemas['ApiResponse_ConversationSummary_']>(
      `/conversations/${encodeURIComponent(conversationId)}/pin`,
      {
        method: 'PATCH',
        body: JSON.stringify({ pinned }),
      },
    )
  ).data
}

/** 客户结束咨询后，后端决定直接结束还是进入坐席收尾。 */
export async function finishConversation(
  conversationId: string,
): Promise<Conversation> {
  return (
    await call<ConversationResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/finish`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      },
    )
  ).data
}

export async function listMessages(
  conversationId: string,
): Promise<ConversationMessage[]> {
  const response = await call<MessageListResponse>(
    `/conversations/${encodeURIComponent(conversationId)}/messages?limit=50`,
  )
  return [...response.data]
}

export async function sendCustomerMessage(
  conversationId: string,
  payload: {
    message_id: string
    content: string
    sent_at: string
    conversation_model_profile: ModelSelection['conversation_model_profile']
    decision_mode: ModelSelection['decision_mode']
  },
  idempotencyKey: string,
): Promise<CustomerTurnResult> {
  return (
    await call<CustomerTurnResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/messages`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': idempotencyKey },
        body: JSON.stringify({
          message_id: payload.message_id,
          content: payload.content,
          sent_at: payload.sent_at,
          conversation_model_profile: payload.conversation_model_profile,
          decision_mode: payload.decision_mode,
        }),
      },
    )
  ).data
}

/** 读取后端允许客户选择的对话模型和决策模式。 */
export async function getModelOptions(): Promise<ModelSelectionCatalog> {
  return (
    await call<Schemas['ApiResponse_ModelSelectionCatalog_']>('/model-options')
  ).data
}

export async function getCustomerTurn(
  conversationId: string,
  turnId: string,
): Promise<CustomerTurnResult> {
  return (
    await call<CustomerTurnResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/turns/${encodeURIComponent(turnId)}`,
    )
  ).data
}

/** 读取已打开会话在当前上下文周期内截至最近一轮的累计 Token 用量。 */
export async function getConversationTokenUsage(
  conversationId: string,
): Promise<TurnTokenUsage | null> {
  return (
    await call<TokenUsageResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/token-usage`,
    )
  ).data
}

export async function listRecommendations(
  conversationId: string,
): Promise<Recommendation[]> {
  return [
    ...(
      await call<RecommendationListResponse>(
        `/conversations/${encodeURIComponent(conversationId)}/recommendations`,
      )
    ).data,
  ]
}

/** 明确处理一份待审核 RAG 草稿；采用、编辑和拒绝共用同一审计入口。 */
export async function reviewRagDraft(
  conversationId: string,
  payload: RagReview,
  key: string,
): Promise<RagReviewResult> {
  return (
    await call<Schemas['ApiResponse_RagReviewResult_']>(
      `/conversations/${encodeURIComponent(conversationId)}/rag/review`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': key },
        body: JSON.stringify(payload),
      },
    )
  ).data
}

export async function recordRecommendationInteraction(
  conversationId: string,
  recommendationId: string,
  action: Schemas['InteractionAction'],
): Promise<void> {
  await call(
    `/conversations/${encodeURIComponent(conversationId)}/recommendations/${encodeURIComponent(recommendationId)}/interactions`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({ action }),
    },
  )
}

export async function submitRecommendationFeedback(
  conversationId: string,
  recommendationId: string,
  helpful: boolean,
): Promise<void> {
  await call(
    `/conversations/${encodeURIComponent(conversationId)}/recommendations/${encodeURIComponent(recommendationId)}/feedback`,
    {
      method: 'POST',
      headers: { 'Idempotency-Key': crypto.randomUUID() },
      body: JSON.stringify({
        adoption_status: helpful ? 'ADOPTED' : 'NOT_ADOPTED',
        helpfulness: helpful ? 'HELPFUL' : 'NOT_HELPFUL',
        reason_codes: [],
      }),
    },
  )
}

export async function createWrapUp(
  conversationId: string,
): Promise<ConversationWrapUp> {
  return (
    await call<WrapUpResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/wrap-up`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
      },
    )
  ).data
}

export async function confirmWrapUp(
  conversationId: string,
  version: string,
): Promise<ConversationWrapUp> {
  return (
    await call<WrapUpResponse>(
      `/conversations/${encodeURIComponent(conversationId)}/wrap-up/confirm`,
      {
        method: 'POST',
        headers: { 'Idempotency-Key': crypto.randomUUID() },
        body: JSON.stringify({ expected_version: Number(version) }),
      },
    )
  ).data
}

export async function streamConversationEvents(
  conversationId: string,
  afterSequence: number,
  signal: AbortSignal,
  onOpen: () => void,
  onEvent: (event: ConversationEvent) => Promise<void>,
): Promise<void> {
  await streamEventSource(
    `/conversations/${encodeURIComponent(conversationId)}/events?after_sequence=${afterSequence}`,
    signal,
    onOpen,
    onEvent,
  )
}

/** 监听当前坐席可查看的全部客户事件；事件只用于提醒，不会替坐席切换会话。 */
export async function streamAgentEvents(
  afterSequence: number,
  signal: AbortSignal,
  onOpen: () => void,
  onEvent: (event: ConversationEvent) => Promise<void>,
): Promise<void> {
  await streamEventSource(
    `/agent/events?after_sequence=${afterSequence}`,
    signal,
    onOpen,
    onEvent,
  )
}

/** 读取一个 SSE 事件流并把完整事件交给页面；消息正文仍从历史接口读取。 */
async function streamEventSource(
  path: string,
  signal: AbortSignal,
  onOpen: () => void,
  onEvent: (event: ConversationEvent) => Promise<void>,
): Promise<void> {
  const token = readSession()?.token
  if (!token) throw new ApiResponseError('登录状态已过期，请重新登录', 401)
  const response = await fetch(`/api/v2${path}`, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'text/event-stream',
    },
    signal,
  })
  if (!response.ok || !response.body) {
    if (response.status === 401) clearSession()
    throw new ApiResponseError(
      response.status === 401
        ? '登录状态已过期，请重新登录'
        : `实时连接失败 (${response.status})`,
      response.status,
    )
  }
  onOpen()
  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffered = ''
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) return
      buffered += decoder
        .decode(value, { stream: true })
        .replaceAll('\r\n', '\n')
      let boundary = buffered.indexOf('\n\n')
      while (boundary !== -1) {
        const frame = buffered.slice(0, boundary)
        buffered = buffered.slice(boundary + 2)
        const data = frame
          .split('\n')
          .filter((line) => line.startsWith('data: '))
          .map((line) => line.slice(6))
          .join('\n')
        if (data) await onEvent(JSON.parse(data) as ConversationEvent)
        boundary = buffered.indexOf('\n\n')
      }
    }
  } finally {
    reader.releaseLock()
  }
}
