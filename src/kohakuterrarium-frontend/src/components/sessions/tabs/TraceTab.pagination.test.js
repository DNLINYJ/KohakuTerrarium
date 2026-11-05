import { flushPromises, shallowMount } from "@vue/test-utils"
import { createPinia, setActivePinia } from "pinia"
import { reactive } from "vue"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

const state = vi.hoisted(() => ({
  detail: null,
  rollup: null,
  timeline: null,
  router: { replace: vi.fn() },
  route: null,
}))

vi.mock("@tanstack/vue-virtual", async () => {
  const { ref } = await import("vue")
  return {
    useVirtualizer: (options) =>
      ref({
        getVirtualItems: () =>
          Array.from({ length: options.value.count }, (_, index) => ({
            index,
            key: options.value.getItemKey(index),
            start: index * 46,
          })),
        getTotalSize: () => 0,
        measureElement: vi.fn(),
        measure: vi.fn(),
        scrollToIndex: vi.fn(),
      }),
  }
})

vi.mock("vue-router", () => ({
  useRoute: () => state.route,
  useRouter: () => state.router,
}))

vi.mock("@/utils/api", () => ({
  sessionAPI: {
    getHistoryIndex: vi.fn(),
    getTree: vi.fn(),
    getSummary: vi.fn(),
    getTurns: vi.fn(),
    getTimeline: vi.fn(),
  },
}))
vi.mock("@/utils/wsUrl", () => ({ wsUrl: (path) => `ws://localhost${path}` }))
vi.mock("@/utils/i18n", () => ({
  useI18n: () => ({ t: (key) => key }),
}))

import TraceTab from "./TraceTab.vue"
import TraceEventDetail from "@/components/sessions/trace/TraceEventDetail.vue"
import TraceTimeline from "@/components/sessions/trace/TraceTimeline.vue"
import SubagentConversationPanel from "@/components/subagents/SubagentConversationPanel.vue"
import TraceFilters from "@/components/sessions/trace/TraceFilters.vue"
import TraceTurnGroup from "@/components/sessions/trace/TraceTurnGroup.vue"
import CostTab from "./CostTab.vue"
import { useSessionDetailStore } from "@/stores/sessionDetail"
import { useTurnRollupStore } from "@/stores/turnRollup"
import { useTraceTimelineStore } from "@/stores/traceTimeline"
import { sessionAPI } from "@/utils/api"

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

class Socket {
  static instances = []
  constructor(url) {
    this.url = url
    Socket.instances.push(this)
  }
  close() {}
  event(event) {
    this.onmessage?.({
      data: JSON.stringify({ type: "event", key: String(event.event_id), event }),
    })
  }
}

const wrappers = []
function mountTrace(scope = null) {
  const wrapper = shallowMount(TraceTab, {
    global: {
      provide: { "kt:scope": scope },
      stubs: { "el-drawer": { template: "<div><slot /></div>" } },
    },
  })
  wrappers.push(wrapper)
  return wrapper
}
function choose(wrapper, changes) {
  const picker = wrapper.findComponent(TraceFilters)
  picker.vm.$emit("update:modelValue", { ...picker.props("modelValue"), ...changes })
}
afterEach(() => {
  wrappers.splice(0).forEach((w) => w.unmount())
  vi.unstubAllGlobals()
})

beforeEach(async () => {
  setActivePinia(createPinia())
  state.route = reactive({ query: {} })
  Socket.instances = []
  vi.stubGlobal("WebSocket", Socket)
  vi.clearAllMocks()
  sessionAPI.getHistoryIndex.mockReset().mockResolvedValue({ meta: {}, targets: ["alice"] })
  sessionAPI.getTree.mockReset().mockResolvedValue(null)
  sessionAPI.getSummary.mockReset().mockResolvedValue({ agents: ["alice"] })
  sessionAPI.getTurns.mockReset().mockImplementation(async (name, { agent, offset }) => ({
    agent,
    turns: [{ turn_index: 1001 }],
    total: 1001,
    offset,
  }))
  sessionAPI.getTimeline.mockReset().mockImplementation(async (name, { agent }) => ({
    agent,
    spans: [{ eid: 42, turn: 5, type: "tool_result", ts: 1 }],
    truncated: false,
  }))
  state.detail = useSessionDetailStore()
  state.rollup = useTurnRollupStore()
  state.timeline = useTraceTimelineStore()
  await state.detail.load("session-a")
  vi.spyOn(state.rollup, "load")
  vi.spyOn(state.rollup, "ensureTurn")
  state.router.replace.mockReset()
})

describe("TraceTab single-attempt discovery", () => {
  it.each([
    [200, 200],
    [404, 200],
    [200, 404],
    [404, 404],
    [500, 500],
    [500, 404],
    [404, 500],
  ])(
    "settles empty/error discovery (%s/%s) without an anonymous pair and permits retry",
    async (summaryCode, indexCode) => {
      const response = (code, data) =>
        code === 200
          ? Promise.resolve(data)
          : Promise.reject({ response: { status: code }, message: `HTTP ${code}` })
      sessionAPI.getSummary.mockImplementationOnce(() =>
        response(summaryCode, { agents: ["ch:team"] }),
      )
      sessionAPI.getHistoryIndex.mockImplementationOnce(() =>
        response(indexCode, { meta: {}, targets: [] }),
      )
      await state.detail.load("empty")
      const wrapper = mountTrace()
      await flushPromises()
      expect(sessionAPI.getTurns).not.toHaveBeenCalled()
      expect(sessionAPI.getTimeline).not.toHaveBeenCalled()
      expect(wrapper.text()).not.toContain("sessionViewer.trace.loading")
      if (summaryCode === 500 || indexCode === 500) {
        expect(wrapper.get('[role="alert"]').text()).toContain("HTTP 500")
        await wrapper.get('[role="alert"] button').trigger("click")
      } else {
        expect(wrapper.text()).toContain("sessionViewer.trace.empty")
        state.detail.requestReload()
      }
      await flushPromises()
      expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual(["alice"])
      expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    },
  )

  it.each(["summary", "index"])(
    "loads once when only %s fails, retaining the discovery failure",
    async (failure) => {
      if (failure === "summary")
        sessionAPI.getSummary.mockRejectedValueOnce(new Error("unavailable"))
      else sessionAPI.getHistoryIndex.mockRejectedValueOnce(new Error("unavailable"))
      await state.detail.load("fallback")
      const wrapper = mountTrace()
      await flushPromises()
      expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual(["alice"])
      expect(state.detail.summaryError || state.detail.error).toContain("unavailable")
      sessionAPI.getSummary.mockResolvedValueOnce({ agents: ["bob"] })
      await state.detail.loadSummary()
      await flushPromises()
      expect(wrapper.findComponent(TraceFilters).props("modelValue").agent).toBe("alice")
      expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
    },
  )

  it("keeps explicit choice over late metadata, dedupes repeated emits, and retries a timeline-only failure once", async () => {
    const summary = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(summary.promise)
    sessionAPI.getTimeline.mockRejectedValueOnce(new Error("timeline offline"))
    sessionAPI.getTurns.mockImplementation(async (name, { agent }) => ({
      agent,
      turns: [{ turn_index: 1 }],
      total: 1,
    }))
    const loading = state.detail.load("explicit")
    const wrapper = mountTrace()
    choose(wrapper, { agent: "alice" })
    await flushPromises()
    choose(wrapper, { agent: "alice" })
    await flushPromises()
    summary.resolve({ agents: ["bob", "alice"] })
    await loading
    await flushPromises()
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent(TraceTurnGroup).exists()).toBe(true)
    expect(wrapper.get('[role="alert"]').text()).toContain("timeline offline")
    await wrapper.get('[role="alert"] button').trigger("click")
    await flushPromises()
    expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual([
      "alice",
      "alice",
    ])
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    choose(wrapper, { agent: "bob" })
    await flushPromises()
    choose(wrapper, { agent: "alice" })
    await flushPromises()
    expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual([
      "alice",
      "alice",
      "bob",
      "alice",
    ])
  })

  it("attaches only to a ready agent and ignores stale socket events after agent changes", async () => {
    const summary = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(summary.promise)
    const loading = state.detail.load("live")
    const wrapper = mountTrace()
    choose(wrapper, { live: true })
    await flushPromises()
    expect(Socket.instances).toHaveLength(0)
    summary.resolve({ agents: ["bob", "alice"] })
    await loading
    await flushPromises()
    expect(Socket.instances.map((s) => s.url)).toEqual([
      "ws://localhost/ws/sessions/live/events?agent=bob",
    ])
    const old = Socket.instances[0]
    old.event({ event_id: 100, type: "text", ts: 1, turn_index: 1 })
    await flushPromises()
    expect(state.timeline.records.map((r) => r.eid)).toContain(100)
    choose(wrapper, { agent: "alice" })
    await flushPromises()
    old.event({ event_id: 101, type: "text", ts: 1, turn_index: 1 })
    Socket.instances[1].event({ event_id: 102, type: "text", ts: 1, turn_index: 1 })
    await flushPromises()
    expect(state.timeline.records.map((r) => r.eid)).toEqual([42, 102])
    state.detail.requestReload()
    await flushPromises()
    expect(Socket.instances).toHaveLength(2)
    expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual([
      "bob",
      "alice",
      "alice",
    ])
  })

  it("invalidates stale A-B-A attempts and opens deep links only on the current rows, independent of other scopes", async () => {
    const firstTurns = deferred(),
      firstTimeline = deferred()
    sessionAPI.getTurns.mockReturnValueOnce(firstTurns.promise)
    sessionAPI.getTimeline.mockReturnValueOnce(firstTimeline.promise)
    const wrapper = mountTrace()
    await flushPromises()
    state.route.query.turn = "5"
    await state.detail.load("B")
    await flushPromises()
    const currentTurns = deferred(),
      currentTimeline = deferred()
    sessionAPI.getTurns.mockReturnValueOnce(currentTurns.promise)
    sessionAPI.getTimeline.mockReturnValueOnce(currentTimeline.promise)
    await state.detail.load("session-a")
    await flushPromises()
    state.rollup.ensureTurn.mockClear()
    firstTurns.resolve({ agent: "alice", turns: [{ turn_index: 5 }], total: 1 })
    firstTimeline.reject(new Error("old timeline"))
    await flushPromises()
    expect(state.rollup.turns).toEqual([])
    expect(state.rollup.loading).toBe(true)
    expect(state.timeline.loading).toBe(true)
    expect(state.timeline.error).toBe("")
    expect(state.rollup.ensureTurn).not.toHaveBeenCalled()
    const otherDetail = useSessionDetailStore("other")
    await otherDetail.load("other-session")
    mountTrace("other")
    await flushPromises()
    expect(state.rollup.loading).toBe(true)
    currentTurns.resolve({ agent: "alice", turns: [{ turn_index: 5 }], total: 1 })
    currentTimeline.resolve({ agent: "alice", spans: [] })
    await flushPromises()
    expect(state.rollup.ensureTurn).toHaveBeenCalledTimes(1)
    expect(wrapper.findComponent(TraceTurnGroup).props("expanded")).toBe(true)
    expect(useTurnRollupStore("other").sessionName).toBe("other-session")
  })

  it("allows a fresh activation without clearing Cost's newer aggregate generation", async () => {
    const trace = mountTrace()
    await flushPromises()
    const aggregate = deferred()
    sessionAPI.getTurns.mockReturnValueOnce(aggregate.promise)
    const cost = shallowMount(CostTab)
    wrappers.push(cost)
    trace.unmount()
    expect(state.rollup.loading).toBe(true)
    expect(state.rollup.aggregate).toBe(true)
    aggregate.resolve({ turns: [{ turn_index: 8, tokens_in: 20 }], total: 1 })
    await flushPromises()
    expect(state.rollup.turns[0].turn_index).toBe(8)
    cost.unmount()
    const fresh = mountTrace()
    await flushPromises()
    expect(state.rollup.aggregate).toBe(false)
    expect(state.rollup.turns[0].turn_index).toBe(1001)
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(2)
    expect(fresh.findComponent(TraceFilters).props("modelValue").agent).toBe("alice")
  })

  it.each(["index", "summary"])(
    "waits for the unfiltered summary with %s first, then keeps one pair through metadata and filter changes",
    async (first) => {
      const index = deferred(),
        summary = deferred(),
        tree = deferred()
      sessionAPI.getHistoryIndex.mockReturnValueOnce(index.promise)
      sessionAPI.getSummary.mockReturnValueOnce(summary.promise)
      sessionAPI.getTree.mockReturnValueOnce(tree.promise)
      sessionAPI.getTurns.mockImplementation(async (name, { agent }) => ({
        agent,
        turns: [{ turn_index: 1 }],
        total: 1,
      }))
      const loading = state.detail.load("discovery")
      const wrapper = mountTrace()
      await flushPromises()
      expect(sessionAPI.getTurns).not.toHaveBeenCalled()
      expect(sessionAPI.getTimeline).not.toHaveBeenCalled()
      if (first === "index") {
        index.resolve({ meta: { viewer_default_agent: "alice" }, targets: ["alice"] })
        await flushPromises()
        expect(sessionAPI.getTurns).not.toHaveBeenCalled()
      }
      summary.resolve({
        agents: ["bob", "attached:worker", "alice"],
        totals: { tokens: { prompt: 20 } },
      })
      await flushPromises()
      expect(sessionAPI.getTurns.mock.calls.map(([, args]) => args.agent)).toEqual(["bob"])
      expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual(["bob"])
      index.resolve({ meta: {}, targets: ["alice"] })
      tree.resolve({ name: "late" })
      await loading
      await flushPromises()
      choose(wrapper, { agent: "bob" })
      await flushPromises()
      choose(wrapper, { agent: "bob", search: "query", errorsOnly: true, typeChips: ["tool"] })
      await flushPromises()
      expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
      expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
      expect(state.detail.summary.totals.tokens.prompt).toBe(20)
      choose(wrapper, { agent: "" })
      await flushPromises()
      expect(wrapper.findComponent(TraceFilters).props("modelValue").agent).toBe("bob")
      expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
      sessionAPI.getSummary.mockResolvedValueOnce({
        agents: ["alice", "bob"],
        totals: { tokens: { prompt: 40 } },
      })
      state.detail.requestReload()
      await flushPromises()
      expect(sessionAPI.getTurns.mock.calls.map(([, args]) => args.agent)).toEqual(["bob", "bob"])
      expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(2)
      expect(state.detail.summary.totals.tokens.prompt).toBe(40)
    },
  )

  it("honors explicit choice while discovery is pending and clearing resumes discovery rather than retaining that choice", async () => {
    const summary = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(summary.promise)
    sessionAPI.getTurns.mockResolvedValue({ agent: "alice", turns: [], total: 0 })
    const loading = state.detail.load("explicit")
    const wrapper = mountTrace()
    choose(wrapper, { agent: "alice" })
    await flushPromises()
    expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual(["alice"])
    choose(wrapper, { agent: "" })
    await flushPromises()
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
    summary.resolve({ agents: ["bob", "alice"] })
    await loading
    await flushPromises()
    expect(sessionAPI.getTimeline.mock.calls.map(([, args]) => args.agent)).toEqual([
      "alice",
      "bob",
    ])
  })
})

describe("TraceTab long-session navigation", () => {
  it.each([1000, 1001])(
    "preserves the %s-turn latest-window and older-page HTTP contract",
    async (total) => {
      sessionAPI.getTurns.mockImplementation(async (name, { agent, offset, limit }) => ({
        agent,
        turns: [{ turn_index: offset === 0 && limit === 1 ? 1 : total }],
        total,
        offset,
      }))
      const wrapper = mountTrace()
      await flushPromises()
      expect(sessionAPI.getTurns.mock.calls.map(([, args]) => args.offset)).toEqual(
        total === 1000 ? [0] : [0, 1],
      )
      expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
      expect(state.rollup.turns[0].turn_index).toBe(total)
      if (total > 1000) {
        const older = wrapper
          .findAll("button")
          .find((b) => b.text() === "sessionViewer.trace.turn.loadEarlier")
        await older.trigger("click")
        await flushPromises()
        expect(sessionAPI.getTurns).toHaveBeenLastCalledWith("session-a", {
          agent: "alice",
          aggregate: false,
          offset: 0,
          limit: 1,
        })
        expect(state.rollup.turns.map((t) => t.turn_index)).toEqual([1, 1001])
      }
    },
  )
  it("reconsiders excluded search turns on refresh and searches the newly selected agent", async () => {
    sessionAPI.getTurns.mockImplementation(async (name, { agent }) => ({
      agent,
      turns: [{ turn_index: 5 }],
      total: 1,
    }))
    const wrapper = mountTrace()
    await flushPromises()
    choose(wrapper, { search: "needle" })
    await flushPromises()
    expect(wrapper.findComponent(TraceTurnGroup).props("expanded")).toBe(true)
    wrapper.findComponent(TraceTurnGroup).vm.$emit("matches", 5, 0)
    await flushPromises()
    expect(wrapper.findComponent(TraceTurnGroup).exists()).toBe(false)
    state.detail.requestReload()
    await flushPromises()
    expect(wrapper.findComponent(TraceTurnGroup).exists()).toBe(true)
    expect(wrapper.findComponent(TraceTurnGroup).props("expanded")).toBe(true)
    choose(wrapper, { agent: "bob" })
    await flushPromises()
    expect(wrapper.findComponent(TraceTurnGroup).props("expanded")).toBe(true)
    expect(wrapper.findComponent(TraceTurnGroup).props("agent")).toBe("bob")
  })
  it("opens a sub-agent conversation without changing the trace agent", async () => {
    const wrapper = mountTrace()
    await flushPromises()

    const filtersBefore = state.rollup.agent
    const loadCallsBefore = state.rollup.load.mock.calls.length
    wrapper.findComponent(TraceEventDetail).vm.$emit("open-conversation", {
      jobId: "agent_explore_11111111",
      name: "explore",
      run: 2,
      parent: "alice",
    })
    await flushPromises()

    const panel = wrapper.findComponent(SubagentConversationPanel)
    expect(panel.exists()).toBe(true)
    expect(panel.props()).toMatchObject({
      sessionId: "session-a",
      parent: "alice",
      jobId: "agent_explore_11111111",
      name: "explore",
      run: 2,
      live: false,
    })
    expect(state.rollup.agent).toBe(filtersBefore)
    expect(state.rollup.load).toHaveBeenCalledTimes(loadCallsBefore)
  })

  it("uses the runtime graph scope for a store-backed conversation in a live Inspector", async () => {
    state.detail.live = true
    state.detail.meta = { session_id: "creature-123" }
    state.detail.name = "graph-456"
    const wrapper = mountTrace()
    await flushPromises()

    wrapper.findComponent(TraceEventDetail).vm.$emit("open-conversation", {
      jobId: "agent_explore_11111111",
      name: "explore",
      run: 2,
      parent: "alice",
    })
    await flushPromises()

    expect(wrapper.findComponent(SubagentConversationPanel).props()).toMatchObject({
      sessionId: "graph-456",
      parent: "alice",
      live: false,
      fill: true,
      showBack: true,
    })

    wrapper.findComponent(SubagentConversationPanel).vm.$emit("back")
    await flushPromises()

    expect(wrapper.findComponent(SubagentConversationPanel).exists()).toBe(false)
    expect(wrapper.findComponent(TraceEventDetail).exists()).toBe(true)
  })

  it("feeds terminal completion ids from turn rollup breakdowns to the detail pane", async () => {
    const turns = [
      {
        turn_index: 1001,
        subagent_breakdown: [
          { job_id: "agent_explore_11111111", has_error: false },
          { job_id: "", has_error: false },
        ],
      },
      { turn_index: 1002 },
    ]
    sessionAPI.getTurns.mockResolvedValue({ agent: "alice", turns, total: 2 })
    const wrapper = mountTrace()
    await flushPromises()

    expect(wrapper.findComponent(TraceEventDetail).props("completedJobIds")).toEqual([
      "agent_explore_11111111",
    ])
  })

  it("resolves a missing turn before routing to a selected timeline span", async () => {
    const wrapper = mountTrace()
    await flushPromises()
    sessionAPI.getTurns.mockResolvedValueOnce({
      agent: "alice",
      turns: [{ turn_index: 5 }],
      total: 1,
    })

    wrapper.findComponent(TraceTimeline).vm.$emit("select-span", {
      turn: 5,
      index: 42,
      member: null,
    })
    await flushPromises()

    expect(state.rollup.ensureTurn).toHaveBeenCalledWith(5)
    expect(state.router.replace).toHaveBeenCalledWith({ query: { turn: 5 } })
  })
})
