<script setup lang="ts">
import {
  IconCheck,
  IconClose,
  IconDown,
  IconDelete,
  IconEdit,
  IconMore,
  IconPlus,
  IconPushpin,
} from '@arco-design/web-vue/es/icon'
import { computed, ref } from 'vue'
import CustomerDetails from './CustomerDetails.vue'
import ServiceConversation from './ServiceConversation.vue'
import type {
  Conversation,
  ConversationMessage,
  ConversationSummary,
  CustomerServiceContext,
  CustomerProfile,
  ModelSelection,
  ModelSelectionCatalog,
  TurnTokenUsage,
} from './api'

const props = defineProps<{
  customer?: CustomerProfile
  context: CustomerServiceContext | null
  contextLoading: boolean
  conversation: Conversation | null
  conversations: ConversationSummary[]
  messages: ConversationMessage[]
  busy: boolean
  sending: boolean
  pending: boolean
  closing: boolean
  tokenUsage: TurnTokenUsage | null
  modelOptions: ModelSelectionCatalog | null
}>()

const emit = defineEmits<{
  start: []
  selectConversation: [conversationId: string]
  refresh: []
  send: [content: string, selection: ModelSelection]
  close: []
  renameConversation: [conversationId: string, title: string]
  togglePinned: [conversationId: string, pinned: boolean]
  refreshTurn: []
  deleteConversation: [conversationId: string]
}>()

const businessVisible = ref(false)
const hasAgentReplies = computed(() =>
  props.messages.some((message) => message.role === 'AGENT'),
)
const editingConversationId = ref('')
const titleDraft = ref('')
const deletingConversation = ref<ConversationSummary | null>(null)
const collapsedDateGroups = ref<string[]>([])

type ConversationDateGroup = {
  key: string
  label: string
  conversations: ConversationSummary[]
}

const dateTime = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))

const statusText: Record<string, string> = {
  ACTIVE: '进行中',
  WRAP_UP: '待完成',
  FINISHED: '已结束',
}
const statusColor: Record<string, string> = {
  ACTIVE: 'arcoblue',
  WRAP_UP: 'orange',
  FINISHED: 'gray',
}

const shortTime = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))

/** 使用浏览器本地日期生成稳定分组键。 */
function conversationDateKey(value: string): string {
  const date = new Date(value)
  const year = date.getFullYear()
  const month = String(date.getMonth() + 1).padStart(2, '0')
  const day = String(date.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

/** 把分组日期显示为便于阅读的中文年月日。 */
function conversationDateLabel(value: string): string {
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: 'long',
    day: 'numeric',
  }).format(new Date(value))
}

const conversationGroups = computed<ConversationDateGroup[]>(() => {
  const groups = new Map<string, ConversationDateGroup>()
  for (const conversation of props.conversations) {
    if (conversation.is_pinned) continue
    const key = conversationDateKey(
      conversation.last_message_at ?? conversation.created_at,
    )
    const current = groups.get(key)
    if (current) {
      current.conversations.push(conversation)
      continue
    }
    groups.set(key, {
      key,
      label: conversationDateLabel(
        conversation.last_message_at ?? conversation.created_at,
      ),
      conversations: [conversation],
    })
  }
  const pinned = props.conversations.filter(
    (conversation) => conversation.is_pinned,
  )
  return pinned.length
    ? [
        { key: 'pinned', label: '置顶', conversations: pinned },
        ...groups.values(),
      ]
    : [...groups.values()]
})

/** 打开当前会话的行内标题编辑框。 */
function beginRename(conversation: ConversationSummary): void {
  editingConversationId.value = conversation.conversation_id
  titleDraft.value = conversation.title
}

/** 关闭编辑框，并放弃尚未保存的内容。 */
function cancelRename(): void {
  editingConversationId.value = ''
  titleDraft.value = ''
}

/** 校验非空标题后交给父页面保存。 */
function saveRename(conversationId: string): void {
  const title = titleDraft.value.trim()
  if (!title) return
  emit('renameConversation', conversationId, title)
  cancelRename()
}

/** 根据省略号菜单选项执行置顶或重命名，避免点击菜单时误选会话。 */
function handleConversationAction(
  action: string | number | Record<string, unknown> | undefined,
  conversation: ConversationSummary,
): void {
  if (action === 'rename') {
    beginRename(conversation)
    return
  }
  if (action === 'pin') {
    emit('togglePinned', conversation.conversation_id, !conversation.is_pinned)
  }
  if (action === 'delete') deletingConversation.value = conversation
}

/** 确认删除已结束咨询后，由父页面调用软删除接口。 */
function confirmDelete(): void {
  const item = deletingConversation.value
  if (item?.status === 'FINISHED')
    emit('deleteConversation', item.conversation_id)
  deletingConversation.value = null
}

/** 展开或收起某一天的会话，不影响其他日期分组。 */
function toggleDateGroup(groupKey: string): void {
  collapsedDateGroups.value = collapsedDateGroups.value.includes(groupKey)
    ? collapsedDateGroups.value.filter((key) => key !== groupKey)
    : [...collapsedDateGroups.value, groupKey]
}

/** 判断日期分组当前是否处于收起状态。 */
function isDateGroupCollapsed(groupKey: string): boolean {
  return collapsedDateGroups.value.includes(groupKey)
}
</script>

<template>
  <a-layout class="workspace-layout customer-workspace">
    <a-layout-sider :width="260" class="workspace-sider">
      <a-card title="最近咨询" class="fill-card recent-card" :bordered="false">
        <a-button
          long
          class="new-conversation-button"
          :loading="busy"
          @click="$emit('start')"
        >
          <template #icon><IconPlus /></template>
          新建咨询
        </a-button>
        <div class="recent-conversation-list">
          <div
            v-if="conversationGroups.length"
            class="conversation-date-groups"
          >
            <section
              v-for="group in conversationGroups"
              :key="group.key"
              class="conversation-date-section"
            >
              <button
                type="button"
                class="conversation-date-toggle"
                :aria-expanded="!isDateGroupCollapsed(group.key)"
                @click="toggleDateGroup(group.key)"
              >
                <span>{{ group.label }}</span>
                <IconDown
                  :class="{
                    'is-collapsed': isDateGroupCollapsed(group.key),
                  }"
                />
              </button>
              <a-list
                v-show="!isDateGroupCollapsed(group.key)"
                :bordered="false"
                :split="false"
                size="small"
              >
                <a-list-item
                  v-for="item in group.conversations"
                  :key="item.conversation_id"
                >
                  <div
                    class="conversation-entry"
                    :class="{
                      'is-selected':
                        conversation?.conversation_id === item.conversation_id,
                    }"
                  >
                    <template
                      v-if="editingConversationId === item.conversation_id"
                    >
                      <a-input
                        v-model="titleDraft"
                        :max-length="30"
                        size="small"
                        allow-clear
                        class="conversation-title-input"
                        @keyup.enter="saveRename(item.conversation_id)"
                        @keyup.esc="cancelRename"
                      />
                      <a-button
                        type="text"
                        size="mini"
                        aria-label="保存标题"
                        @click="saveRename(item.conversation_id)"
                      >
                        <IconCheck />
                      </a-button>
                      <a-button
                        type="text"
                        size="mini"
                        aria-label="取消重命名"
                        @click="cancelRename"
                      >
                        <IconClose />
                      </a-button>
                    </template>
                    <template v-else>
                      <button
                        class="conversation-entry-select"
                        @click="
                          $emit('selectConversation', item.conversation_id)
                        "
                      >
                        <strong class="conversation-entry-title">
                          <IconPushpin v-if="item.is_pinned" />
                          <span>{{ item.title }}</span>
                        </strong>
                        <span class="conversation-entry-meta">
                          <small>{{
                            shortTime(item.last_message_at ?? item.created_at)
                          }}</small>
                          <a-tag
                            :color="statusColor[item.status]"
                            size="small"
                            >{{ statusText[item.status] ?? item.status }}</a-tag
                          >
                        </span>
                      </button>
                      <a-dropdown
                        trigger="click"
                        @select="
                          (action) => handleConversationAction(action, item)
                        "
                      >
                        <a-button
                          type="text"
                          size="mini"
                          class="conversation-menu-button"
                          aria-label="会话操作"
                        >
                          <IconMore />
                        </a-button>
                        <template #content>
                          <a-doption value="pin">
                            <span class="conversation-menu-option">
                              <IconPushpin />
                              <span>{{
                                item.is_pinned ? '取消置顶' : '置顶'
                              }}</span>
                            </span>
                          </a-doption>
                          <a-doption value="rename">
                            <span class="conversation-menu-option">
                              <IconEdit />
                              <span>重命名</span>
                            </span>
                          </a-doption>
                          <a-doption value="delete">
                            <span
                              class="conversation-menu-option conversation-delete-option"
                            >
                              <IconDelete /><span>删除</span>
                            </span>
                          </a-doption>
                        </template>
                      </a-dropdown>
                    </template>
                  </div>
                </a-list-item>
              </a-list>
            </section>
          </div>
          <a-empty
            v-if="!conversations.length && !busy"
            description="暂无咨询记录"
            class="recent-empty"
          />
        </div>
      </a-card>
    </a-layout-sider>

    <a-layout-content class="workspace-main">
      <ServiceConversation
        :customer-name="customer?.display_name"
        :conversation="conversation"
        :messages="messages"
        :busy="busy"
        :sending="sending"
        :pending="pending"
        :token-usage="tokenUsage"
        :model-options="modelOptions"
        @start="$emit('start')"
        @refresh="$emit('refresh')"
        @send="(content, selection) => $emit('send', content, selection)"
        @refresh-turn="$emit('refreshTurn')"
      />
    </a-layout-content>

    <a-layout-sider :width="328" class="workspace-sider details-sider">
      <a-card
        title="当前服务"
        class="fill-card current-service-card"
        :bordered="false"
      >
        <div class="current-service-content">
          <template v-if="conversation">
            <div class="service-fact">
              <span>咨询编号</span>
              <strong class="service-number">{{
                conversation.conversation_id
              }}</strong>
            </div>
            <div class="service-fact">
              <span>发起时间</span>
              <strong>{{ dateTime(conversation.created_at) }}</strong>
            </div>
            <div class="service-fact service-agent-fact">
              <span>人工服务</span>
              <a-tag
                :color="hasAgentReplies ? 'arcoblue' : 'gray'"
                size="small"
                >{{ hasAgentReplies ? '已有人工意见' : '暂无人工意见' }}</a-tag
              >
            </div>
            <div class="service-fact">
              <span>坐席工号</span>
              <strong class="service-placeholder">等待接入</strong>
            </div>
            <a-divider />
            <p class="service-guidance">
              {{
                conversation.status === 'ACTIVE'
                  ? '您可以在中间的咨询区继续沟通。'
                  : '本次咨询已停止接收新消息，您仍可查看完整记录。'
              }}
            </p>
            <!-- 浮层留在固定应用容器内，避免挂到 body 后触发页面级滚动条抖动。 -->
            <a-popconfirm
              v-if="conversation.status === 'ACTIVE'"
              content="结束后将不能继续发送消息，确定结束当前咨询吗？"
              position="left"
              popup-container=".app-shell"
              @ok="$emit('close')"
            >
              <a-button
                long
                status="danger"
                :loading="closing"
                :disabled="pending || sending"
              >
                结束当前咨询
              </a-button>
            </a-popconfirm>
          </template>
          <p v-else class="service-guidance">
            发起咨询后，这里会显示当前服务的进展。
          </p>

          <a-divider />
          <div class="service-business-entry">
            <strong>我的业务</strong>
            <a-button type="text" @click="businessVisible = true">
              查看
            </a-button>
          </div>

          <div
            class="service-safety"
            :class="{ 'with-conversation': conversation }"
          >
            <strong>安全提示</strong>
            <a-alert type="info">
              请勿在咨询中提供账户密码、短信验证码或完整身份证号码。
            </a-alert>
          </div>
        </div>
      </a-card>
    </a-layout-sider>

    <a-modal
      :visible="Boolean(deletingConversation)"
      title="删除咨询记录"
      ok-text="删除"
      :ok-button-props="{
        status: 'danger',
        disabled: deletingConversation?.status !== 'FINISHED',
      }"
      @cancel="deletingConversation = null"
      @ok="confirmDelete"
    >
      <p>
        删除“{{
          deletingConversation?.title
        }}”后，该咨询将从你的列表移除，历史和业务审计仍保留。
      </p>
      <p v-if="deletingConversation?.status !== 'FINISHED'">
        这条咨询尚未结束，请先结束咨询再删除。
      </p>
    </a-modal>
    <a-drawer
      v-model:visible="businessVisible"
      title="我的业务"
      width="min(480px, 100vw)"
      :footer="false"
      class="business-drawer"
    >
      <CustomerDetails
        :context="context"
        :loading="contextLoading"
        :agent="false"
      />
    </a-drawer>
  </a-layout>
</template>
