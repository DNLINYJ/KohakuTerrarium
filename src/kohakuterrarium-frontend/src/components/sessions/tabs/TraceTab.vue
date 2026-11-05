<template>
  <div class="h-full min-h-0 flex flex-col gap-3 overflow-hidden">
    <!-- Filters (V3) + Live attach toggle (V4) -->
    <TraceFilters v-model="filterModel" :agents="agents" :live-status="liveStatus" />

    <!-- Timeline (V3): lane overview with drag-to-focus interval -->
    <TraceTimeline v-if="timelineModel" :model="timelineModel" v-model:mode="timelineMode" v-model:range="timelineRange" :truncated="timeline.truncated" @select-span="onSelectSpan" />

    <!-- Focus chip + fold-all controls -->
    <div v-if="timelineRange || rollup.turns.length" class="flex items-center gap-2 text-[11px]">
      <span v-if="timelineRange" class="px-2 py-0.5 rounded-full bg-iolite/15 text-iolite font-mono flex items-center gap-1.5">
        {{ t("sessionViewer.trace.timeline.focusTurns", { n: displayedTurns.length }) }}
        <button class="i-carbon-close hover:text-coral" :title="t('sessionViewer.trace.timeline.clearFocus')" @click="timelineRange = null" />
      </span>
      <button v-if="rollup.hasOlder" class="text-iolite hover:underline disabled:opacity-50" :disabled="rollup.loadingOlder" @click="loadOlderTurns">
        {{ rollup.loadingOlder ? "…" : t("sessionViewer.trace.turn.loadEarlier") }}
      </button>
      <span class="flex-1" />
      <button class="text-warm-500 hover:text-warm-700 dark:hover:text-warm-300" @click="expandAll">{{ t("sessionViewer.trace.turn.expandAll") }}</button>
      <button class="text-warm-500 hover:text-warm-700 dark:hover:text-warm-300" @click="collapseAll">{{ t("sessionViewer.trace.turn.collapseAll") }}</button>
    </div>

    <div v-if="attemptError" class="card p-4 text-coral text-sm" role="alert">
      {{ attemptError }}
      <button class="ml-2 text-iolite hover:underline" @click="retry">{{ t("common.retry") }}</button>
    </div>
    <div v-if="effectiveAgent && (rollup.loading || timeline.loading) && rollup.turns.length" class="text-secondary text-sm" role="status">{{ t("sessionViewer.trace.loading") }}</div>
    <div v-if="rollup.pageError" class="text-[11px] text-coral">{{ rollup.pageError }}</div>

    <!-- Live "↓ N new" banner (V4) -->
    <button v-if="filters.live && newSinceLastClear > 0 && !atBottom" class="self-end px-3 py-1 rounded-full bg-aquamarine/15 text-aquamarine text-[11px] font-mono shadow-md hover:bg-aquamarine/25" @click="scrollToBottom">{{ t("sessionViewer.trace.live.newBanner", { n: newSinceLastClear }) }}</button>

    <!-- Turn list (virtualized) -->
    <div ref="scrollEl" class="flex-1 min-h-0 overflow-y-auto" @scroll="onScroll">
      <div v-if="(!effectiveAgent && detail.traceDiscovery.status === 'pending') || (rollup.loading && !rollup.turns.length)" class="card p-4 text-secondary text-sm">{{ t("sessionViewer.trace.loading") }}</div>
      <div v-else-if="!rollup.turns.length && !attemptError" class="card p-4 text-secondary text-sm">{{ t("sessionViewer.trace.empty") }}</div>

      <div v-else-if="rollup.turns.length" class="relative" :style="{ height: `${totalSize}px` }">
        <div v-for="vRow in virtualRows" :key="vRow.key" :data-index="vRow.index" :ref="measureRow" class="absolute top-0 left-0 w-full pb-1.5" :style="{ transform: `translateY(${vRow.start}px)` }">
          <TraceTurnGroup :turn="displayedTurns[vRow.index]" :agent="rollup.agent" :session-name="detail.name" :expanded="expandedTurns.has(displayedTurns[vRow.index].turn_index)" :filters="filters" :live-events="liveEventsObjects" :selected-event-id="selectedEventId" :focus-event-ids="focusEventIds" :jump-event-id="jumpIdFor(displayedTurns[vRow.index].turn_index)" :jump-member="jumpMemberFor(displayedTurns[vRow.index].turn_index)" @toggle="onToggle" @select-event="onSelectEvent" @matches="onTurnMatches" @jumped="onJumped" />
        </div>
      </div>
    </div>

    <!-- Event detail drawer -->
    <el-drawer v-model="detailOpen" :title="t('sessionViewer.detail.title')" direction="rtl" size="40%" :modal="false" :destroy-on-close="false">
      <div class="h-full min-h-0 flex flex-col">
        <SubagentConversationPanel v-if="conversationRef" :session-id="conversationSessionId" :parent="conversationRef.parent" :job-id="conversationRef.jobId" :name="conversationRef.name" :run="conversationRef.run" :live="false" fill show-back @back="onCloseConversation" />
        <TraceEventDetail v-else :event="selectedEvent" :parent-agent="rollup.agent || filters.agent" :completed-job-ids="completedJobIds" @open-conversation="onOpenSubagent" />
      </div>
    </el-drawer>
  </div>
</template>

<script setup>
import { useVirtualizer } from "@tanstack/vue-virtual"
import { computed, nextTick, onUnmounted, ref, watch } from "vue"
import { useRoute, useRouter } from "vue-router"

import TraceEventDetail from "@/components/sessions/trace/TraceEventDetail.vue"
import SubagentConversationPanel from "@/components/subagents/SubagentConversationPanel.vue"
import TraceTab_TraceFilters from "@/components/sessions/trace/TraceFilters.vue"
import TraceTab_TraceTimeline from "@/components/sessions/trace/TraceTimeline.vue"
import TraceTab_TraceTurnGroup from "@/components/sessions/trace/TraceTurnGroup.vue"
import { isTraceErrorEvent } from "@/components/sessions/trace/traceErrors"
import { parseSearchTerms } from "@/components/sessions/trace/traceSearch"
import { deriveTraceTimeline, traceTimelineFocus } from "@/components/sessions/trace/traceTimeline"
import { useSessionEventStream } from "@/composables/useSessionEventStream"
import { useEventStreamStore } from "@/stores/eventStream"
import { useSessionDetailStore } from "@/stores/sessionDetail"
import { useTraceTimelineStore } from "@/stores/traceTimeline"
import { useTurnRollupStore } from "@/stores/turnRollup"
import { useI18n } from "@/utils/i18n"

const TraceFilters = TraceTab_TraceFilters
const TraceTimeline = TraceTab_TraceTimeline
const TraceTurnGroup = TraceTab_TraceTurnGroup

const { t } = useI18n()
const detail = useSessionDetailStore()
const rollup = useTurnRollupStore()
const timeline = useTraceTimelineStore()
const stream = useEventStreamStore()
const liveStream = useSessionEventStream()
const route = useRoute()
const router = useRouter()

const filters = ref({
  agent: "",
  errorsOnly: false,
  typeChips: [],
  live: false,
  search: "",
})

const explicitAgent = ref("")
const defaultAgent = ref("")
const effectiveAgent = computed(() => explicitAgent.value || defaultAgent.value)
const filterModel = computed({
  get: () => filters.value,
  set: (value) => {
    if (value.agent !== filters.value.agent) explicitAgent.value = value.agent || ""
    filters.value = { ...value, agent: explicitAgent.value || defaultAgent.value }
  },
})
const retryGeneration = ref(0)
const dispatchedKey = ref("")
let bindingKey = ""
let activeAgent = ""
let rollupGeneration = null
let timelineGeneration = null
const attemptError = computed(() => (!effectiveAgent.value ? detail.traceDiscovery.error : rollup.error || timeline.error) || "")

function retry() {
  if (!effectiveAgent.value) detail.requestReload()
  else retryGeneration.value++
}

function currentTurns() {
  return !!effectiveAgent.value && rollup.loadGeneration === rollupGeneration && !rollup.loading && !rollup.error && rollup.sessionName === detail.name && rollup.agent === effectiveAgent.value && !rollup.aggregate
}

const expandedTurns = ref(new Set())
const scrollEl = ref(null)
const atBottom = ref(true)

// Lane-timeline focus: projection mode + selected interval in its domain.
const timelineMode = ref("sequence")
const timelineRange = ref(null)

const timelineModel = computed(() => deriveTraceTimeline(timeline.records, timelineMode.value))

const timelineFocus = computed(() => {
  if (!timelineRange.value || !timelineModel.value) return null
  return traceTimelineFocus(timelineModel.value, timelineRange.value)
})

const focusEventIds = computed(() => timelineFocus.value?.eventIds ?? null)

// Event search (Phase 3): auto-expand turns while a query is active so
// their events load and can be filtered; groups report match counts.
// Declared before ``displayedTurns`` — the virtualizer evaluates it
// eagerly during setup.
const searchActive = computed(() => parseSearchTerms(filters.value.search).length > 0)
const zeroMatchTurns = ref(new Set())

// Event-detail panel state.
const selectedEvent = ref(null)
const conversationRef = ref(null)
const detailOpen = ref(false)
const conversationSessionId = computed(() => detail.name)
const selectedEventId = computed(() => (selectedEvent.value && typeof selectedEvent.value.event_id === "number" ? selectedEvent.value.event_id : null))

function onSelectEvent(ev) {
  selectedEvent.value = ev
  conversationRef.value = null
  detailOpen.value = true
}

function onOpenSubagent(reference) {
  if (!reference) return
  conversationRef.value = reference
}

function onCloseConversation() {
  conversationRef.value = null
}

// Jobs with a persisted terminal outcome anywhere in the session.
// Rollup ``subagent_breakdown`` rows are written only once a run
// finished (managed or synthetic), covering every turn without
// requiring any turn to be expanded client-side.
const completedJobIds = computed(() => {
  const ids = []
  for (const turn of rollup.turns || []) {
    for (const row of turn.subagent_breakdown || []) {
      const jobId = row?.job_id
      if (jobId && !ids.includes(jobId)) ids.push(jobId)
    }
  }
  return ids
})

const agents = computed(() => detail.agents || [])

const liveEvents = liveStream.events
const newSinceLastClear = liveStream.newSinceLastClear

const liveStatus = computed(() => {
  if (!filters.value.live) return ""
  if (liveStream.error.value) return liveStream.error.value
  if (liveStream.subscribed.value) return t("sessionViewer.trace.live.subscribed")
  return t("sessionViewer.trace.live.connecting")
})

// Convert {key, event} → event objects for the per-turn group rendering.
const liveEventsObjects = computed(() => liveEvents.value.map((e) => e.event))
const liveErrorTurns = computed(
  () =>
    new Set(
      liveEventsObjects.value
        .filter(isTraceErrorEvent)
        .map((event) => event.turn_index)
        .filter((turn) => Number.isInteger(turn)),
    ),
)

// "Errors only" reflects in the turn list as well as the timeline.
// Persisted rollups and current live events both contribute failures.
const displayedTurns = computed(() => {
  let turns = rollup.turns
  if (filters.value.errorsOnly) {
    turns = turns.filter((t2) => t2.has_error || liveErrorTurns.value.has(t2.turn_index))
  }
  const focus = timelineFocus.value
  if (focus) turns = turns.filter((t2) => focus.turns.has(t2.turn_index))
  // While searching, turns whose loaded events have zero matches are
  // hidden (reported by the turn groups via ``matches``).
  if (searchActive.value && zeroMatchTurns.value.size) {
    turns = turns.filter((t2) => !zeroMatchTurns.value.has(t2.turn_index))
  }
  return turns
})

// Virtualized turn list: one row per turn, dynamically measured so
// expanded groups can grow arbitrarily tall.
const rowVirtualizer = useVirtualizer(
  computed(() => ({
    count: displayedTurns.value.length,
    getScrollElement: () => scrollEl.value,
    estimateSize: () => 46,
    getItemKey: (i) => `${rollup.agent}-${displayedTurns.value[i]?.turn_index ?? i}`,
    overscan: 10,
  })),
)
const virtualRows = computed(() => rowVirtualizer.value.getVirtualItems())
const totalSize = computed(() => rowVirtualizer.value.getTotalSize())
const measureRow = (el) => rowVirtualizer.value.measureElement(el)

function onTurnMatches(turnIndex, count) {
  const next = new Set(zeroMatchTurns.value)
  if (count === 0) next.add(turnIndex)
  else next.delete(turnIndex)
  zeroMatchTurns.value = next
}

watch(
  () => filters.value.search,
  (q) => {
    // Re-querying must un-hide turns so their groups remount + re-report.
    zeroMatchTurns.value = new Set()
    if (parseSearchTerms(q).length) expandAll()
  },
)

function expandAll() {
  expandedTurns.value = new Set(displayedTurns.value.map((t2) => t2.turn_index))
}

function collapseAll() {
  expandedTurns.value = new Set()
}

function onToggle(turnIndex) {
  if (expandedTurns.value.has(turnIndex)) {
    expandedTurns.value.delete(turnIndex)
  } else {
    expandedTurns.value.add(turnIndex)
  }
  // Force reactive update — Set mutation isn't tracked.
  expandedTurns.value = new Set(expandedTurns.value)
}

async function onSelectTurn(turnIndex, { updateRoute = true } = {}) {
  if (!currentTurns()) return false
  const generation = rollup.loadGeneration
  if (!(await rollup.ensureTurn(turnIndex)) || !currentTurns() || generation !== rollup.loadGeneration) return false
  // Selecting a turn outside the focused interval clears the focus, like
  // the reference trajectory UI — otherwise the target would stay hidden.
  if (timelineFocus.value && !timelineFocus.value.turns.has(turnIndex)) {
    timelineRange.value = null
  }
  expandedTurns.value = new Set([...expandedTurns.value, turnIndex])
  if (updateRoute) router.replace({ query: { ...route.query, turn: turnIndex } })
  await nextTick()
  if (!currentTurns() || generation !== rollup.loadGeneration) return false
  const index = displayedTurns.value.findIndex((t2) => t2.turn_index === turnIndex)
  if (index >= 0) rowVirtualizer.value.scrollToIndex(index, { align: "center" })
  return true
}

// Timeline span click: expand the span's turn, then hand the event id to
// that turn's group ONLY — it scrolls the exact event row into view once
// loaded (auto-paging through the turn's event pages when needed). The
// target must not reach other groups: they would exhaust immediately and
// clear it before the real group finishes loading.
const jumpTarget = ref(null)

function jumpIdFor(turnIndex) {
  const t2 = jumpTarget.value
  return t2 && t2.turn === turnIndex ? t2.eid : null
}

function jumpMemberFor(turnIndex) {
  const t2 = jumpTarget.value
  return t2 && t2.turn === turnIndex ? t2.member : null
}

async function onSelectSpan(span) {
  if (!span || span.turn == null) return
  jumpTarget.value = typeof span.index === "number" ? { turn: span.turn, eid: span.index, member: span.member ?? null } : null
  if (!(await onSelectTurn(span.turn))) jumpTarget.value = null
}

function onJumped({ event }) {
  jumpTarget.value = null
  if (event) selectedEvent.value = event
}

function onScroll() {
  const el = scrollEl.value
  if (!el) return
  atBottom.value = el.scrollTop + el.clientHeight >= el.scrollHeight - 16
  if (atBottom.value && newSinceLastClear.value > 0) {
    liveStream.clearNewCounter()
  }
}

function scrollToBottom() {
  const el = scrollEl.value
  if (!el) return
  el.scrollTop = el.scrollHeight
  liveStream.clearNewCounter()
}

async function loadOlderTurns() {
  if (await rollup.loadOlder()) {
    await nextTick()
    rowVirtualizer.value.measure()
  }
}

// Forward live events into the active turn's stream-store so
// expanded turn groups update in real time without a refetch.
watch(
  () => liveEvents.value[liveEvents.value.length - 1],
  (last) => {
    if (!filters.value.live || !effectiveAgent.value || timeline.loadGeneration !== timelineGeneration) return
    if (last?.event) {
      stream.appendLive(last.event)
      timeline.appendLive(last.event)
    }
  },
)

function resetSelection() {
  timelineRange.value = null
  zeroMatchTurns.value = new Set()
  expandedTurns.value = new Set()
  selectedEvent.value = null
  conversationRef.value = null
  detailOpen.value = false
  jumpTarget.value = null
}

watch(
  () => [detail.name, detail.bindingGeneration, detail.loadEpoch, detail.traceDiscovery.status, detail.traceDiscovery.agent, explicitAgent.value, retryGeneration.value],
  () => {
    const binding = JSON.stringify([detail.name, detail.bindingGeneration])
    if (binding !== bindingKey) {
      bindingKey = binding
      explicitAgent.value = ""
      defaultAgent.value = ""
      activeAgent = ""
      dispatchedKey.value = ""
      filters.value = { ...filters.value, agent: "" }
      rollup.clear()
      timeline.clear()
      resetSelection()
    }
    if (!explicitAgent.value && !defaultAgent.value && detail.traceDiscovery.status === "ready") defaultAgent.value = detail.traceDiscovery.agent
    const agent = effectiveAgent.value
    filters.value = { ...filters.value, agent }
    if (agent !== activeAgent) {
      activeAgent = agent
      rollup.clear()
      timeline.clear()
      resetSelection()
    }
    if (!detail.name || !agent) {
      dispatchedKey.value = ""
      return
    }
    const key = JSON.stringify([binding, agent, detail.loadEpoch, retryGeneration.value])
    if (dispatchedKey.value === key) return
    dispatchedKey.value = key
    timelineRange.value = null
    zeroMatchTurns.value = new Set()
    rollup.load(detail.name, agent)
    rollupGeneration = rollup.loadGeneration
    timeline.load(detail.name, agent)
    timelineGeneration = timeline.loadGeneration
  },
  { immediate: true },
)

watch(
  () => rollup.turns,
  () => {
    if (currentTurns() && searchActive.value) expandAll()
  },
)

onUnmounted(() => {
  if (rollup.loadGeneration === rollupGeneration) rollup.clear()
  if (timeline.loadGeneration === timelineGeneration) timeline.clear()
})

// Live attach on/off.
watch(
  () => JSON.stringify([filters.value.live, detail.name, detail.bindingGeneration, effectiveAgent.value]),
  () => {
    liveStream.detach()
    liveEvents.value = []
    if (filters.value.live && detail.name && effectiveAgent.value) {
      liveStream.attach(detail.name, effectiveAgent.value)
    }
  },
)

// Deep-link: ``?turn=N`` opens that turn group.
watch(
  () => [route.query.turn, rollup.loading, dispatchedKey.value],
  async ([q]) => {
    if (q == null || !currentTurns()) return
    const ti = Number(q)
    if (!Number.isFinite(ti)) return
    await onSelectTurn(ti, { updateRoute: false })
  },
  { immediate: true },
)
</script>
