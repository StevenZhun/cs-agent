<script setup lang="ts">
import { computed } from 'vue'
import type { CustomerServiceContext } from './api'

const props = defineProps<{
  context: CustomerServiceContext | null
  loading: boolean
  agent: boolean
}>()

const records = computed(() => props.context?.records ?? [])
const customerStatusText: Record<string, string> = {
  ACTIVE: '服务中',
  INACTIVE: '已停用',
}
const recordStatusText: Record<string, string> = {
  OPEN: '处理中',
  PENDING: '待确认',
  RESOLVED: '已解决',
  CLOSED: '已关闭',
}
const recordStatusColor: Record<string, string> = {
  OPEN: 'arcoblue',
  PENDING: 'orange',
  RESOLVED: 'green',
  CLOSED: 'gray',
}

const dateTime = (value: string) =>
  new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(value))
</script>

<template>
  <div class="business-details">
    <template v-if="context">
      <div class="profile-line">
        <a-avatar :size="42">{{
          context.customer.display_name.slice(0, 1)
        }}</a-avatar>
        <div class="profile-name">
          <strong>{{ context.customer.display_name }}</strong>
          <span>{{ context.customer.masked_contact }}</span>
        </div>
        <a-tag
          v-if="agent"
          :color="context.customer.status === 'ACTIVE' ? 'green' : 'gray'"
        >
          {{ customerStatusText[context.customer.status] ?? '待核实' }}
        </a-tag>
      </div>

      <a-card size="small" class="profile-card">
        <a-descriptions :column="2" layout="vertical" size="small">
          <a-descriptions-item label="服务等级">
            {{ context.customer.service_level }}
          </a-descriptions-item>
          <a-descriptions-item label="客户标签">
            {{
              context.customer.tags.length
                ? context.customer.tags.join('、')
                : '无'
            }}
          </a-descriptions-item>
        </a-descriptions>
      </a-card>

      <div class="details-section-heading">
        <strong>{{ agent ? '服务记录' : '我的服务记录' }}</strong>
        <a-tag>{{ records.length }} 条</a-tag>
      </div>
      <a-card
        v-for="record in records"
        :key="record.record_id"
        size="small"
        class="record-card"
      >
        <template #title>{{ record.title }}</template>
        <template #extra>
          <a-tag :color="recordStatusColor[record.status] ?? 'gray'">
            {{ recordStatusText[record.status] ?? '待核实' }}
          </a-tag>
        </template>
        <a-descriptions :column="2" layout="vertical" size="small">
          <a-descriptions-item label="服务类别">
            {{ record.category }}
          </a-descriptions-item>
          <a-descriptions-item label="更新时间">
            {{ dateTime(record.updated_at) }}
          </a-descriptions-item>
          <a-descriptions-item v-if="record.reference_code" label="参考编号">
            {{ record.reference_code }}
          </a-descriptions-item>
        </a-descriptions>
      </a-card>
      <a-empty
        v-if="!records.length"
        :description="agent ? '暂无服务记录' : '您目前没有服务记录'"
        class="inline-empty"
      />
    </template>
    <div v-else-if="loading" class="details-loading"><a-spin /></div>
    <a-empty
      v-else
      :description="agent ? '选择客户后查看客户资料' : '暂无客户资料'"
      class="details-empty"
    />
  </div>
</template>
