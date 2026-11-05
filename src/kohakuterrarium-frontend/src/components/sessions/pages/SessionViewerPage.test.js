/**
 * SessionViewerPage tests.
 *
 * The first two describes keep the page's store/route scheduling focused
 * (SessionDetail is a leaf stub) and preserve the original Drives probe
 * race coverage. The "real viewer workflow" describe mounts the actual
 * SessionDetail → ConvTab / SessionHistoryViewer / TraceTab / CostTab
 * hierarchy with the real Pinia stores; only visual leaves (ChatPanel,
 * TraceTimeline, TraceTurnGroup, Element Plus widgets) are stubbed, so
 * every network request asserted below is orchestration produced by the
 * production components.
 */

import { mount, flushPromises } from "@vue/test-utils"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"
import { createPinia, setActivePinia } from "pinia"
import { defineComponent, h, reactive } from "vue"
import { useChatStore } from "@/stores/chat"
import { useSessionDetailStore } from "@/stores/sessionDetail"
import { useTurnRollupStore } from "@/stores/turnRollup"

let testRoute
const routerMock = { push: vi.fn(), replace: vi.fn() }

vi.mock("vue-router", () => ({
  useRoute: () => testRoute,
  useRouter: () => routerMock,
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

vi.mock("@/utils/drivesApi", () => {
  const api = { savedList: vi.fn(), list: vi.fn() }
  return { drivesAPI: api, default: api }
})

vi.mock("@/utils/api", () => {
  const sessionAPI = {
    getHistoryIndex: vi.fn(),
    getTree: vi.fn(),
    getSummary: vi.fn(),
    getHistoryPage: vi.fn(),
    getTurns: vi.fn(),
    getTimeline: vi.fn(),
    getEvents: vi.fn(),
    getHistoryDetail: vi.fn(),
  }
  // The real chat / trace hierarchy imports these peers; only sessionAPI
  // is exercised here.
  return { default: {}, sessionAPI, agentAPI: {}, terrariumAPI: {} }
})

import SessionViewerPage from "./SessionViewerPage.vue"
import { drivesAPI } from "@/utils/drivesApi"
import { sessionAPI } from "@/utils/api"

function deferred() {
  let resolve, reject
  const promise = new Promise((done, fail) => {
    resolve = done
    reject = fail
  })
  return { promise, resolve, reject }
}

function historyPage(target, content = `${target} message`) {
  return {
    messages: [{ role: "user", content, _history_key: "s1" }],
    events: [],
    history_page: {
      version: 1,
      stream: "snapshot",
      history_id: `h-${target}`,
      before: "s1",
      after: "s1",
      has_older: false,
      has_newer: false,
      reset_required: false,
    },
    live_job_ids: [],
  }
}

const detailStub = { name: "SessionDetail", template: "<div data-test='detail-stub' />" }
const treeStub = { name: "SessionTreePane", template: "<div />" }

// Leaf-only probes: the transcript renderer and the virtualized/canvas
// widgets are not orchestration. ChatPanelProbe still reads the real
// per-session chat store so history pages can be asserted end-to-end.
const ChatPanelProbe = defineComponent({
  name: "ChatPanel",
  props: { instance: { type: Object, default: null } },
  setup(props) {
    const chat = useChatStore(props.instance.id)
    return () =>
      h(
        "div",
        { "data-test": "chat-panel" },
        (chat.messagesByTab?.[chat.activeTab] || []).map((m) => m.content).join("|"),
      )
  },
})

const realLeafStubs = {
  ChatPanel: ChatPanelProbe,
  TraceTimeline: { name: "TraceTimeline", template: "<div data-test='trace-timeline' />" },
  TraceTurnGroup: { name: "TraceTurnGroup", template: "<div data-test='trace-turn' />" },
  "el-checkbox": {
    name: "ElCheckbox",
    props: ["modelValue"],
    template: "<label><slot /></label>",
  },
  "el-select": { name: "ElSelect", template: "<div><slot /></div>" },
  "el-option": { name: "ElOption", template: "<div />" },
  "el-drawer": {
    name: "ElDrawer",
    props: ["modelValue"],
    template: "<div v-if='modelValue'><slot /></div>",
  },
}

const stubbedDetail = {
  SessionDetail: detailStub,
  SessionTreePane: treeStub,
}

const wrappers = []
function mountPage(options = {}, { real = true } = {}) {
  const global = options.global || {}
  const stubs = {
    ...(real ? realLeafStubs : stubbedDetail),
    ...(global.stubs || {}),
  }
  const wrapper = mount(SessionViewerPage, {
    ...options,
    global: { ...global, stubs },
  })
  wrappers.push(wrapper)
  return wrapper
}

async function clickTab(wrapper, label) {
  const button = wrapper.findAll("button").find((b) => b.text().trim() === label)
  if (!button) throw new Error(`tab button not found: ${label}`)
  await button.trigger("click")
  await flushPromises()
}

let storage
beforeEach(() => {
  // The locale store reads ui-prefs from localStorage on init; jsdom has no
  // real one, so provide an in-memory shim (matches the other component tests).
  storage = new Map()
  vi.stubGlobal("localStorage", {
    getItem: (k) => (storage.has(k) ? storage.get(k) : null),
    setItem: (k, v) => storage.set(k, String(v)),
    removeItem: (k) => storage.delete(k),
    clear: () => storage.clear(),
  })
  setActivePinia(createPinia())
  testRoute = reactive({ params: {}, query: {} })
  // SessionHistoryViewer relies on the auto-imported vue-router helpers
  // (it has no explicit import), so the globals must resolve in tests.
  vi.stubGlobal("useRoute", () => testRoute)
  vi.stubGlobal("useRouter", () => routerMock)
  routerMock.push.mockReset()
  routerMock.replace.mockReset()

  sessionAPI.getHistoryIndex.mockReset().mockResolvedValue({ meta: {}, targets: [] })
  sessionAPI.getTree.mockReset().mockResolvedValue(null)
  sessionAPI.getSummary.mockReset().mockResolvedValue({
    agents: ["alice"],
    totals: { tokens: { prompt: 5000, completion: 1200, cached: 300 }, cost_usd: null },
  })
  sessionAPI.getHistoryPage
    .mockReset()
    .mockImplementation(async (_name, target) => historyPage(target))
  sessionAPI.getTurns.mockReset().mockImplementation(async (_name, { agent } = {}) => ({
    agent,
    turns: [{ turn_index: 1, tokens_in: 100, tokens_out: 20, tokens_cached: 10 }],
    total: 1,
    offset: 0,
  }))
  sessionAPI.getTimeline.mockReset().mockImplementation(async (_name, { agent } = {}) => ({
    agent,
    spans: [],
    truncated: false,
  }))
  drivesAPI.savedList.mockReset()
  drivesAPI.list.mockReset()
})

afterEach(() => {
  wrappers.splice(0).forEach((w) => {
    try {
      w.unmount()
    } catch {
      /* already unmounted */
    }
  })
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe("SessionViewerPage — initial saved Conversation", () => {
  it("keeps embedded scopes independent of the routed viewer's tab query", async () => {
    const first = useSessionDetailStore("embed-a")
    const second = useSessionDetailStore("embed-b")
    first.setTab("conv")
    second.setTab("conv")
    drivesAPI.savedList.mockResolvedValue({ drives: [] })
    mountPage({ props: { sessionNameProp: "embed-a" } }, { real: false })
    mountPage({ props: { sessionNameProp: "embed-b" } }, { real: false })
    await flushPromises()
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    testRoute.query.tab = "cost"
    await flushPromises()
    expect(first.activeTab).toBe("conv")
    expect(second.activeTab).toBe("conv")
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    await first.setTab("trace")
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("embed-a", null)
    expect(second.activeTab).toBe("conv")
  })

  it("uses the deep-linked tab before its first request and loads summary on later activation only", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    drivesAPI.savedList.mockResolvedValue({ drives: [] })
    const w = mountPage({}, { real: false })
    await flushPromises()
    const detail = useSessionDetailStore("saved")
    expect(detail.activeTab).toBe("conv")
    expect(sessionAPI.getHistoryIndex).toHaveBeenCalledWith("saved")
    expect(sessionAPI.getTree).toHaveBeenCalledWith("saved")
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    detail.requestReload()
    await flushPromises()
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    const epoch = detail.loadEpoch
    testRoute.query.tab = "trace"
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("saved", null)
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "alice" })
    testRoute.query.tab = "conv"
    await flushPromises()
    testRoute.query.tab = "cost"
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(detail.loadEpoch).toBe(epoch)
    testRoute.params.name = "other"
    testRoute.query.tab = "conv"
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(detail.name).toBe("other")
    w.unmount()
  })

  it("retains live Conversation summaries on initial load and refresh", async () => {
    testRoute.query.tab = "conv"
    drivesAPI.list.mockResolvedValue({ drives: [] })
    useSessionDetailStore("live").setTab("conv")
    const w = mountPage({ props: { sessionNameProp: "live", live: true } }, { real: false })
    await flushPromises()
    const detail = useSessionDetailStore("live")
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("live", null)
    detail.requestReload()
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
    w.unmount()
  })
})

describe("SessionViewerPage — real viewer workflow", () => {
  it("opens ?tab=conv with zero summary and loads only legal history targets", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({
      meta: {},
      targets: ["root", "alice", "root"],
      viewer_agents: ["root:attached:review:0"],
    })
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    const w = mountPage()
    await flushPromises()

    const detail = useSessionDetailStore("saved")
    const chat = useChatStore("session:saved")
    expect(detail.activeTab).toBe("conv")
    expect(detail.loadEpoch).toBe(1)
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    // Duplicate history targets are retained exactly as the index lists
    // them; attached viewer agents are not history targets.
    expect(chat.tabs).toEqual(["root", "alice", "root"])
    expect(chat.activeTab).toBe("root")
    expect(sessionAPI.getHistoryPage).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getHistoryPage).toHaveBeenCalledWith(
      "saved",
      "root",
      expect.objectContaining({ limit: 400 }),
    )
    const historyTargets = sessionAPI.getHistoryPage.mock.calls.map((call) => call[1])
    expect(historyTargets).not.toContain("root:attached:review:0")
    expect(w.get('[data-test="chat-panel"]').text()).toBe("root message")
  })

  it("applies a session + deep-linked tab change before the first request", async () => {
    testRoute.params.name = "first"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockImplementation(async (name) => ({
      meta: {},
      targets: [name],
    }))
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    mountPage()
    await flushPromises()
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    const historyCallsBeforeSwitch = sessionAPI.getHistoryPage.mock.calls.length

    // Name and tab change in the same route update: the watcher must load
    // "second" already on the trace tab, not borrow first's conv tab.
    testRoute.params.name = "second"
    testRoute.query.tab = "trace"
    await flushPromises()

    const scoped = useSessionDetailStore("first")
    expect(scoped.name).toBe("second")
    expect(scoped.activeTab).toBe("trace")
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("second", null)
    expect(scoped.traceDiscovery).toEqual({ status: "ready", agent: "alice" })
    // The trace deep link is applied before the first request, so the
    // second session never mounts a Conversation/history reader.
    const afterSwitch = sessionAPI.getHistoryPage.mock.calls.slice(historyCallsBeforeSwitch)
    expect(afterSwitch).toHaveLength(0)
  })

  it("refreshes the saved conversation without requesting a summary", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: ["root"] })
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    mountPage()
    await flushPromises()
    const detail = useSessionDetailStore("saved")
    const chat = useChatStore("session:saved")
    expect(chat.messagesByTab.root).toHaveLength(1)

    detail.requestReload()
    await flushPromises()

    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    expect(sessionAPI.getHistoryPage).toHaveBeenCalledTimes(2)
    const [, target, options] = sessionAPI.getHistoryPage.mock.calls.at(-1)
    expect(target).toBe("root")
    expect(options).toMatchObject({ after: "s1", stream: "snapshot" })
    expect(chat.messagesByTab.root).toHaveLength(1)
  })

  it("switches conv → Trace with summary priority and one valid request pair", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({
      meta: { agents: ["alice"] },
      targets: ["alice", "alice", "bob"],
      viewer_agents: ["alice:attached:review:0"],
    })
    sessionAPI.getSummary.mockResolvedValue({
      agents: ["bob", "alice"],
      totals: { tokens: { prompt: 5000, completion: 1200, cached: 300 }, cost_usd: null },
    })
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    mountPage()
    await flushPromises()
    expect(sessionAPI.getSummary).not.toHaveBeenCalled()
    // The conversation tab keeps duplicated index rows; only the detail
    // agents getter dedupes.
    expect(useChatStore("session:saved").tabs).toEqual(["alice", "alice", "bob"])

    testRoute.query.tab = "trace"
    await flushPromises()

    const detail = useSessionDetailStore("saved")
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("saved", null)
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "bob" })
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTurns.mock.calls[0][1]).toMatchObject({ agent: "bob" })
    expect(sessionAPI.getTimeline.mock.calls[0][1]).toMatchObject({ agent: "bob" })
    expect(sessionAPI.getHistoryPage.mock.calls.map((call) => call[1])).not.toContain(
      "alice:attached:review:0",
    )
  })

  it("shares one pending summary across repeated tab activation without duplicating it", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: ["alice"] })
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    const pending = deferred()
    sessionAPI.getSummary.mockReturnValueOnce(pending.promise)

    mountPage()
    await flushPromises()
    testRoute.query.tab = "trace"
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTurns).not.toHaveBeenCalled()

    for (const tab of ["conv", "trace", "conv", "trace"]) {
      testRoute.query.tab = tab
      await flushPromises()
    }
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTurns).not.toHaveBeenCalled()
    expect(sessionAPI.getTimeline).not.toHaveBeenCalled()

    pending.resolve({ agents: ["bob"] })
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
    expect(useSessionDetailStore("saved").traceDiscovery).toEqual({
      status: "ready",
      agent: "bob",
    })
  })

  it("keeps Cost aggregate totals from summary even when per-turn rows are partial", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: ["alice"] })
    sessionAPI.getSummary.mockResolvedValue({
      agents: ["alice"],
      totals: { tokens: { prompt: 5000, completion: 1200, cached: 300 }, cost_usd: 0.75 },
    })
    sessionAPI.getTurns.mockImplementation(async (_name, { agent, aggregate } = {}) => ({
      agent,
      turns: [{ turn_index: 1, tokens_in: 100, tokens_out: 20, tokens_cached: 10, cost_usd: 0.01 }],
      total: 1,
      offset: 0,
      aggregate,
    }))
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    const w = mountPage()
    await flushPromises()
    testRoute.query.tab = "cost"
    await flushPromises()

    const detail = useSessionDetailStore("saved")
    const rollup = useTurnRollupStore("saved")
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("saved", null)
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTurns.mock.calls.every((call) => call[1].aggregate === true)).toBe(true)
    expect(rollup.turns).toHaveLength(1)
    expect(w.text()).toContain("5.0k")
    expect(w.text()).toContain("1.2k")
    expect(w.text()).toContain("$0.75")
    expect(detail.summary.totals.tokens.prompt).toBe(5000)
  })

  it("returns to conv and re-enters Trace without another summary in the same epoch", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: ["alice"] })
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    mountPage()
    await flushPromises()
    testRoute.query.tab = "trace"
    await flushPromises()
    const detail = useSessionDetailStore("saved")
    const epoch = detail.loadEpoch
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)

    testRoute.query.tab = "conv"
    await flushPromises()
    testRoute.query.tab = "trace"
    await flushPromises()

    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(detail.loadEpoch).toBe(epoch)
    expect(sessionAPI.getTurns.mock.calls.map((call) => call[1].agent)).toEqual(["alice", "alice"])
  })

  it("retries a failed summary discovery from the Trace alert and then loads one pair", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: [] })
    sessionAPI.getSummary.mockRejectedValueOnce(
      Object.assign(new Error("HTTP 500"), { response: { status: 500 } }),
    )
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    const w = mountPage()
    await flushPromises()
    testRoute.query.tab = "trace"
    await flushPromises()

    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTurns).not.toHaveBeenCalled()
    const alert = w.get('[role="alert"]')
    expect(alert.text()).toContain("HTTP 500")
    await alert.get("button").trigger("click")
    await flushPromises()

    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTimeline.mock.calls[0][1]).toMatchObject({ agent: "alice" })
    expect(w.find('[role="alert"]').exists()).toBe(false)
  })

  it("falls back to a legal index target on a 404 summary and retries it on re-entry", async () => {
    testRoute.params.name = "saved"
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: ["alice"] })
    sessionAPI.getSummary.mockRejectedValueOnce(
      Object.assign(new Error("HTTP 404"), { response: { status: 404 } }),
    )
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    const w = mountPage()
    await flushPromises()
    testRoute.query.tab = "trace"
    await flushPromises()

    const detail = useSessionDetailStore("saved")
    expect(detail.summaryStatus).toBe("not-found")
    expect(detail.traceDiscovery).toEqual({ status: "ready", agent: "alice" })
    expect(w.find('[role="alert"]').exists()).toBe(false)
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(1)
    expect(sessionAPI.getTimeline).toHaveBeenCalledTimes(1)

    testRoute.query.tab = "conv"
    await flushPromises()
    testRoute.query.tab = "trace"
    await flushPromises()

    // A 404 summary is retried (there is no successful cache), while the
    // index fallback keeps the trace usable in the meantime.
    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
    expect(detail.summaryStatus).toBe("success")
    expect(sessionAPI.getTurns).toHaveBeenCalledTimes(2)
  })

  it("keeps A/B scoped detail and chat stores independent across A → B → A", async () => {
    testRoute.query.tab = "conv"
    sessionAPI.getHistoryIndex.mockImplementation(async (name) => ({
      meta: {},
      targets: [name === "A" ? "alice" : "bob"],
    }))
    sessionAPI.getSummary.mockImplementation(async (name) => ({
      agents: [name === "A" ? "alice" : "bob"],
    }))
    drivesAPI.savedList.mockResolvedValue({ drives: [] })

    useSessionDetailStore("A").setTab("conv")
    useSessionDetailStore("B").setTab("conv")
    const a = mountPage({ props: { sessionNameProp: "A" } })
    const b = mountPage({ props: { sessionNameProp: "B" } })
    await flushPromises()

    expect(useChatStore("session:A").tabs).toEqual(["alice"])
    expect(useChatStore("session:B").tabs).toEqual(["bob"])
    expect(useChatStore("session:A").messagesByTab.alice[0].content).toBe("alice message")
    expect(useChatStore("session:B").messagesByTab.bob[0].content).toBe("bob message")

    await clickTab(a, "Trace")
    expect(sessionAPI.getSummary.mock.calls.map((call) => call[0])).toEqual(["A"])
    await clickTab(b, "Trace")
    expect(sessionAPI.getSummary.mock.calls.map((call) => call[0])).toEqual(["A", "B"])

    const detailA = useSessionDetailStore("A")
    const detailB = useSessionDetailStore("B")
    expect(detailA.name).toBe("A")
    expect(detailB.name).toBe("B")
    expect(detailA.summaryAgents).toEqual(["alice"])
    expect(detailB.summaryAgents).toEqual(["bob"])
    // Leaving conv unmounts the transcript; returning reloads only A's
    // scoped chat store with A's history.
    expect(useChatStore("session:A").messagesByTab.alice).toBeUndefined()

    const epochA = detailA.loadEpoch
    await clickTab(a, "Conversation")
    expect(useChatStore("session:A").messagesByTab.alice[0].content).toBe("alice message")
    await clickTab(a, "Trace")
    expect(sessionAPI.getSummary.mock.calls.map((call) => call[0])).toEqual(["A", "B"])
    expect(detailA.loadEpoch).toBe(epochA)
    expect(detailA.name).toBe("A")
    expect(useSessionDetailStore("B").name).toBe("B")
  })

  it("refreshes a live summary header while staying on Cost", async () => {
    testRoute.query.tab = "conv"
    drivesAPI.list.mockResolvedValue({ drives: [] })
    sessionAPI.getHistoryIndex.mockResolvedValue({ meta: {}, targets: ["alice"] })
    sessionAPI.getTurns.mockImplementation(async (_name, { agent, aggregate } = {}) => ({
      agent,
      turns: [{ turn_index: 1, tokens_in: 100, tokens_out: 20, tokens_cached: 10, cost_usd: 0.01 }],
      total: 1,
      offset: 0,
      aggregate,
    }))
    sessionAPI.getSummary.mockResolvedValueOnce({
      agents: ["alice"],
      totals: { tokens: { prompt: 5000, completion: 1200, cached: 300 }, cost_usd: 0.5 },
    })

    useSessionDetailStore("L").setTab("conv")
    const w = mountPage({ props: { sessionNameProp: "L", live: true } })
    await flushPromises()
    expect(sessionAPI.getSummary).toHaveBeenCalledExactlyOnceWith("L", null)

    await clickTab(w, "Cost")
    expect(w.text()).toContain("5.0k")
    expect(w.text()).toContain("1.2k")
    expect(w.text()).toContain("$0.50")

    sessionAPI.getSummary.mockResolvedValueOnce({
      agents: ["alice"],
      totals: { tokens: { prompt: 8000, completion: 2000, cached: 400 }, cost_usd: 0.8 },
    })
    useSessionDetailStore("L").requestReload()
    await flushPromises()

    expect(sessionAPI.getSummary).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain("8.0k")
    expect(w.text()).toContain("2.0k")
    expect(w.text()).toContain("$0.80")
  })
})

describe("SessionViewerPage — Drives probe race", () => {
  it("ignores a stale probe that resolves after switching sessions", async () => {
    const dA = deferred()
    const dB = deferred()
    drivesAPI.savedList.mockImplementation((name) => {
      if (name === "A") return dA.promise
      if (name === "B") return dB.promise
      return Promise.resolve({ drives: [] })
    })

    const w = mountPage({ props: { sessionNameProp: "A" } }, { real: false })
    await flushPromises() // probe A started, still pending

    // Switch to B before A's response arrives.
    await w.setProps({ sessionNameProp: "B" })
    await flushPromises() // probe B started, still pending

    // Current session B resolves first with NO drives → tab stays hidden.
    dB.resolve({ drives: [] })
    await flushPromises()
    expect(w.text()).not.toContain("Drives")

    // The stale A response arrives LATE with drives; without the generation
    // guard it would flip B's tab visible. It must be discarded.
    dA.resolve({ drives: [{ drive_id: "d1" }] })
    await flushPromises()
    expect(w.text()).not.toContain("Drives")
    expect(drivesAPI.savedList).toHaveBeenCalledWith("A")
    expect(drivesAPI.savedList).toHaveBeenCalledWith("B")
  })

  it("shows the Drives tab when the current session has persisted drives", async () => {
    drivesAPI.savedList.mockResolvedValue({ drives: [{ drive_id: "d1" }] })
    const w = mountPage({ props: { sessionNameProp: "A" } }, { real: false })
    await flushPromises()
    expect(w.text()).toContain("Drives")
  })

  it("hides the Drives tab when the current session has none", async () => {
    drivesAPI.savedList.mockResolvedValue({ drives: [] })
    const w = mountPage({ props: { sessionNameProp: "A" } }, { real: false })
    await flushPromises()
    expect(w.text()).not.toContain("Drives")
  })

  it("binds the viewer to a live session id like any other source (UXI-01)", async () => {
    // The inspector embeds this page with the LIVE session id as the
    // name; the viewer must load it through the same detail path as a
    // saved session (an active session already persists a store).
    drivesAPI.savedList.mockResolvedValue({ drives: [] })
    mountPage({ props: { sessionNameProp: "live_graph_7" } }, { real: false })
    await flushPromises()
    expect(sessionAPI.getHistoryIndex).toHaveBeenCalledWith("live_graph_7")
    expect(sessionAPI.getSummary).toHaveBeenCalledWith("live_graph_7", null)
  })

  it("probes the LIVE drives route (not the saved sidecar) when live (item 17)", async () => {
    // A live session's saved sidecar read returns [] under the writer
    // lock, so the tab would wrongly hide. Bound live, the probe must hit
    // the live list — and real live rows surface the tab.
    drivesAPI.list.mockResolvedValue({ drives: [{ drive_id: "d1" }] })
    const w = mountPage({ props: { sessionNameProp: "live_graph_7", live: true } }, { real: false })
    await flushPromises()
    expect(drivesAPI.list).toHaveBeenCalledWith("live_graph_7")
    expect(drivesAPI.savedList).not.toHaveBeenCalled()
    expect(w.text()).toContain("Drives")
  })

  it("still probes the saved sidecar for a saved (non-live) session", async () => {
    drivesAPI.savedList.mockResolvedValue({ drives: [{ drive_id: "d1" }] })
    const w = mountPage({ props: { sessionNameProp: "saved_x" } }, { real: false })
    await flushPromises()
    expect(drivesAPI.savedList).toHaveBeenCalledWith("saved_x")
    expect(drivesAPI.list).not.toHaveBeenCalled()
    expect(w.text()).toContain("Drives")
  })
})
