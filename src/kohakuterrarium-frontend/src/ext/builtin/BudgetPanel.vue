<template>
  <div class="p-3 flex flex-col gap-2 text-xs" data-test="budget-panel">
    <div v-if="!owner.state.value?.configured" class="text-warm-400">The budget plugin has no limits configured on this creature.</div>
    <template v-else>
      <div v-for="axis in axes" :key="axis.name" class="flex flex-col gap-1" :data-axis="axis.name">
        <div class="flex items-center justify-between">
          <span class="font-medium text-warm-700 dark:text-warm-300">{{ axis.name }}</span>
          <span class="font-mono text-warm-500">{{ format(axis.used) }} / {{ axis.hard ? format(axis.hard) : "∞" }}</span>
        </div>
        <el-progress :percentage="percent(axis)" :status="axis.last_alarm === 'hard' || axis.last_alarm === 'crash' ? 'exception' : axis.last_alarm === 'soft' ? 'warning' : ''" :show-text="false" />
      </div>
      <div v-if="owner.state.value.hard_walled" class="text-coral">Hard wall reached: tools are blocked.</div>
    </template>
  </div>
</template>

<script setup>
import { computed } from "vue"

import { useOwner } from "@/ext/sdk/index"

const owner = useOwner()

const axes = computed(() => Object.values(owner.state.value?.axes || {}))

function percent(axis) {
  if (!axis.hard) return 0
  return Math.min(100, Math.round((axis.used / axis.hard) * 100))
}

function format(value) {
  return Number.isInteger(value) ? String(value) : Number(value).toFixed(1)
}
</script>
