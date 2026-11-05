<template>
  <component :is="isCompact ? PhoneSheet : ModalShell" class="kt-v2" :title="thread.title || 'By the way'" :full="true" body-class="p-3" role="dialog" aria-modal="true" :aria-label="thread.title || 'By the way'" data-test="side-conversation-modal" @close="$emit('close')">
    <template #title
      ><span class="flex items-center gap-2 text-iolite dark:text-iolite-light"><span class="i-carbon-chat" />{{ thread.title || "By the way" }}</span></template
    >
    <div ref="content" tabindex="-1" class="h-[65dvh] min-h-0 flex flex-col gap-3 kt-conversation-host">
      <div class="text-xs text-warm-600 dark:text-warm-300">From {{ thread.parent }} · captured context</div>
      <p class="m-0 text-xs text-warm-500 dark:text-warm-400">A separate conversation. Closing this window does not stop it or send its answer to the parent chat.</p>
      <SubagentConversationPanel :key="thread.key" :session-id="thread.sessionId" :parent="thread.parent" :job-id="thread.job_id" :name="thread.name || 'fork'" :run="thread.run" :status="thread.status" live side-conversation fill @metadata="onMetadata" />
    </div>
    <template #footer
      ><div class="flex items-center gap-2 text-xs text-warm-500 dark:text-warm-400">
        <span>Reopen:</span><code class="select-all break-all">/btw --open {{ thread.job_id }}</code>
      </div></template
    >
  </component>
</template>

<script setup>
import { nextTick, onMounted, onUnmounted, ref } from "vue"

import ModalShell from "@/components/common/ModalShell.vue"
import PhoneSheet from "@/components/session-v2/phone/PhoneSheet.vue"
import SubagentConversationPanel from "@/components/subagents/SubagentConversationPanel.vue"
import { useDensity } from "@/composables/useDensity"
import "@/components/session-v2/surfaces.css"

const props = defineProps({ thread: { type: Object, required: true } })
const emit = defineEmits(["close", "metadata"])
function onMetadata(update) {
  const thread = props.thread
  if (update.sessionId !== thread.sessionId || update.parent !== thread.parent || update.jobId !== thread.job_id) return
  emit("metadata", { thread, meta: update.meta })
}
const { isCompact } = useDensity()
const content = ref(null)
const previousFocus = document.activeElement
const focusableSelector = 'button:not(:disabled), textarea:not(:disabled), input:not(:disabled), a[href], [tabindex="0"]'
function dialog() {
  return content.value?.closest('[role="dialog"]')
}
function onKeydown(event) {
  if (event.key === "Escape" && !isCompact.value) {
    event.preventDefault()
    event.stopImmediatePropagation()
    emit("close")
  }
  if (event.key !== "Tab") return
  const items = [...(dialog()?.querySelectorAll(focusableSelector) || [])]
  const first = items[0] || content.value
  const last = items.at(-1) || first
  if (!dialog()?.contains(document.activeElement) || (event.shiftKey && document.activeElement === first) || (!event.shiftKey && document.activeElement === last)) {
    event.preventDefault()
    ;(event.shiftKey ? last : first)?.focus()
  }
}
onMounted(() => {
  window.addEventListener("keydown", onKeydown, true)
  nextTick(() => (dialog()?.querySelector(focusableSelector) || content.value)?.focus())
})
onUnmounted(() => {
  window.removeEventListener("keydown", onKeydown, true)
  if (previousFocus?.isConnected) previousFocus.focus()
})
</script>
