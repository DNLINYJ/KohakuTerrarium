<template>
  <div v-if="side.remembered.value.length" class="shrink-0 px-4 py-1 flex flex-wrap items-center gap-2 text-xs">
    <button v-for="thread in side.remembered.value" :key="thread.key" class="rounded-full px-3 py-2 bg-iolite/10 text-iolite dark:text-iolite-light disabled:opacity-50" :disabled="side.reopening.value" :title="`/btw --open ${thread.job_id}`" @click="side.reopen(thread)"><span class="i-carbon-chat mr-1" />{{ thread.title || "By the way" }}</button>
    <span v-if="side.error.value" role="alert" class="text-coral">{{ side.error.value }}</span>
  </div>
  <SideConversationModal v-if="side.active.value" :thread="side.active.value" @close="side.close" @metadata="side.updateMetadata" />
</template>

<script setup>
import SideConversationModal from "@/components/subagents/SideConversationModal.vue"

defineProps({ side: { type: Object, required: true } })
</script>
