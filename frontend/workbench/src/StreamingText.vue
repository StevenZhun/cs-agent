<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{ text: string; animate: boolean }>()
const emit = defineEmits<{ progress: [] }>()
const visibleText = ref(props.text)
let frame = 0
let completionTimer = 0

/** 完整消息已收到后逐字展开；不会把模板答复冒充模型的 Token 增量。 */
function revealText(): void {
  cancelAnimationFrame(frame)
  clearTimeout(completionTimer)
  if (
    !props.animate ||
    window.matchMedia('(prefers-reduced-motion: reduce)').matches
  ) {
    visibleText.value = props.text
    return
  }
  const characters = Array.from(props.text)
  const duration = Math.min(2000, Math.max(500, characters.length * 12))
  const started = performance.now()
  visibleText.value = characters.slice(0, 1).join('')
  /** 每帧增加一段文字；切换会话或卸载时取消，不遗留定时任务。 */
  function advance(now: number): void {
    const count = Math.min(
      characters.length,
      Math.ceil((characters.length * (now - started)) / duration),
    )
    visibleText.value = characters.slice(0, count).join('')
    emit('progress')
    if (count < characters.length) {
      frame = requestAnimationFrame(advance)
    } else {
      clearTimeout(completionTimer)
    }
  }
  frame = requestAnimationFrame(advance)
  // 浏览器可能因标签切换或系统节能暂停动画帧；兜底定时器保证消息不会永久空白。
  completionTimer = window.setTimeout(() => {
    cancelAnimationFrame(frame)
    visibleText.value = props.text
    emit('progress')
  }, duration + 250)
}

watch([() => props.text, () => props.animate], revealText, { immediate: true })
onBeforeUnmount(() => {
  cancelAnimationFrame(frame)
  clearTimeout(completionTimer)
})
</script>

<template>
  <span>{{ visibleText }}</span>
</template>
