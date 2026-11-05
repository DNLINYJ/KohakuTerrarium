/**
 * Session-detail store — owns the active session being browsed in the
 * Session Viewer. Loads ``meta`` (via the existing history-index
 * endpoint), ``tree`` (V1) and ``summary`` (V1) on demand.
 *
 * Tab state lives here too so deep-linking via ``?tab=trace`` survives
 * a refresh and switching tabs doesn't refetch the tree / summary.
 *
 * **Per-scope** (scope = session name). Two macro-shell session-viewer
 * tabs for different saved sessions each get their own bucket and
 * don't trample each other's tab state / loaded meta. Outside a
 * provider (v1 page route) the default-scope store keeps the
 * historical singleton behaviour.
 */

import { defineStore } from "pinia"
import { getCurrentInstance } from "vue"

import { injectScope, disposeScopeStore, registerScopeDisposer } from "@/composables/useScope"
import { sessionAPI } from "@/utils/api"

const VALID_TABS = new Set(["overview", "trace", "conv", "cost", "find", "diff", "drives"])
const summaryRequests = new WeakMap()
let nextRequestId = 0
const validTab = (tab) => (VALID_TABS.has(tab) ? tab : "overview")

function agentNames(names) {
  return [
    ...new Set(names.filter((name) => typeof name === "string" && name && !name.startsWith("ch:"))),
  ]
}

function requestIdentity(store, counter) {
  const name = store.name
  const binding = store.bindingGeneration
  const epoch = store.loadEpoch
  const request = ++nextRequestId
  store[counter] = request
  return () =>
    store.name === name &&
    store.bindingGeneration === binding &&
    store.loadEpoch === epoch &&
    store[counter] === request
}

const _sessionDetailOptions = {
  state: () => ({
    name: "",
    activeTab: "overview",
    meta: null,
    targets: [],
    viewerAgents: [],
    unfilteredSummary: null,
    tree: null,
    summary: null,
    bindingGeneration: 0,
    loadEpoch: 0,
    metaRequest: 0,
    treeRequest: 0,
    summaryRequest: 0,
    discoveryRequest: 0,
    metaStatus: "idle",
    summaryStatus: "idle",
    summaryAgents: [],
    summaryError: "",
    loadingMeta: false,
    loadingTree: false,
    loadingSummary: false,
    error: "",
    // True when the bound session is LIVE (the inspector embeds the
    // viewer for a running graph): the Drives tab then reads the live
    // ``/sessions/{sid}/drives`` route, whose offline saved counterpart
    // returns [] under the live writer lock (UXI-01). Saved viewers keep
    // it false and read the persisted sidecar.
    live: false,
    // Bumped by ``requestReload`` to tell the per-tab loaders (Cost /
    // Trace / Conversation) to refetch their data as a LIVE session
    // progresses. Never bumped for a saved session (UXI-01 live viewer).
    reloadKey: 0,
  }),

  getters: {
    // Enumerate EVERY creature in the graph, not just the root. The
    // no-agent ``/summary`` lists all creatures (+ attached namespaces)
    // and the history-index ``targets`` are the exact source the
    // conversation dropdown uses; union both (summary first for the
    // viewer-default ordering) and fall back to ``meta.agents`` before
    // either has loaded (UXI-03). Channel targets (``ch:``) are excluded.
    agents: (state) => {
      const names = []
      const seen = new Set()
      const add = (n) => {
        if (typeof n === "string" && n && !n.startsWith("ch:") && !seen.has(n)) {
          seen.add(n)
          names.push(n)
        }
      }
      for (const n of state.summaryAgents) add(n)
      for (const n of state.viewerAgents) add(n)
      for (const n of (state.summary && state.summary.agents) || []) add(n)
      for (const n of state.targets || []) add(n)
      for (const n of (state.meta && state.meta.agents) || []) add(n)
      return names
    },
    traceDiscovery(state) {
      if (["idle", "pending"].includes(state.summaryStatus)) return { status: "pending", agent: "" }
      const summaryAgent = state.summaryAgents[0]
      if (state.summaryStatus === "success" && summaryAgent)
        return { status: "ready", agent: summaryAgent }
      if (["idle", "pending"].includes(state.metaStatus)) return { status: "pending", agent: "" }
      const candidates =
        state.metaStatus === "success"
          ? agentNames([...state.targets, ...(state.meta?.agents || [])])
          : []
      const preferred = state.meta?.viewer_default_agent
      const agent = candidates.includes(preferred) ? preferred : candidates[0]
      if (agent) return { status: "ready", agent }
      const error = state.summaryError || state.error
      return { status: error ? "error" : "empty", agent: "", error }
    },
    needsSummary: (state) => state.live || state.activeTab !== "conv",
    primaryAgent() {
      return this.agents[0] || null
    },
    formatVersion: (state) => state.meta && state.meta.format_version,
    isMigrated: (state) => state.meta && state.meta.format_version === 1,
  },

  actions: {
    setTab(tab) {
      this.activeTab = validTab(tab)
      if (this.name && this.needsSummary) return this.ensureSummary()
    },

    ensureSummary() {
      if (!this.name) return Promise.resolve()
      if (this.summaryStatus === "success") {
        requestIdentity(this, "summaryRequest")
        this.summary = this.unfilteredSummary
        this.loadingSummary = false
        return Promise.resolve()
      }
      const pending = summaryRequests.get(this)
      if (pending?.epoch === this.loadEpoch && pending.name === this.name) {
        const current = requestIdentity(this, "summaryRequest")
        return pending.promise.then(() => {
          if (!current()) return
          this.summary = this.summaryStatus === "success" ? this.unfilteredSummary : null
          this.loadingSummary = false
        })
      }
      return this.loadSummary()
    },

    /**
     * Refresh the viewer's data in place as a live session advances:
     * refetch meta / tree / summary (Overview) and bump ``reloadKey`` so
     * the per-tab loaders (Cost / Trace / Conversation) refetch too.
     * ``load`` keeps the existing meta/summary while refetching (no
     * ``isSwitch``), so there is no spinner flash / scroll reset.
     */
    requestReload() {
      this.reloadKey++
      if (this.name) this.load(this.name)
    },

    async load(name, { tab = this.activeTab } = {}) {
      if (!name) return
      this.activeTab = validTab(tab)
      summaryRequests.delete(this)
      const isSwitch = name !== this.name
      this.name = name
      this.loadEpoch++
      this.metaStatus = "idle"
      this.summaryStatus = "idle"
      this.unfilteredSummary = null
      this.loadingSummary = false
      this.summaryAgents = []
      this.summaryError = ""
      if (isSwitch) {
        this.bindingGeneration++
        this.meta = null
        this.targets = []
        this.viewerAgents = []
        this.tree = null
        this.summary = null
        this.error = ""
      }
      const requests = [this.loadMeta(), this.loadTree()]
      if (this.needsSummary) requests.push(this.ensureSummary())
      else this.summary = null
      await Promise.all(requests)
    },

    async loadMeta() {
      if (!this.name) return
      const name = this.name
      const current = requestIdentity(this, "metaRequest")
      this.loadingMeta = true
      this.metaStatus = "pending"
      this.error = ""
      try {
        const data = await sessionAPI.getHistoryIndex(name)
        if (!current()) return
        this.meta = data.meta || null
        this.targets = data.targets || []
        this.viewerAgents = agentNames(data.viewer_agents || [])
        this.metaStatus = "success"
      } catch (err) {
        if (!current()) return
        this.metaStatus = err?.response?.status === 404 ? "not-found" : "error"
        // A just-started / unpersisted live session has no saved index
        // yet — a 404 is a benign empty state, not a load failure.
        if (err?.response?.status === 404) {
          this.meta = null
          this.targets = []
          this.viewerAgents = []
        } else {
          this.error = `Failed to load session metadata: ${err.message || err}`
        }
      } finally {
        if (current()) this.loadingMeta = false
      }
    },

    async loadTree() {
      if (!this.name) return
      const name = this.name
      const current = requestIdentity(this, "treeRequest")
      this.loadingTree = true
      try {
        const data = await sessionAPI.getTree(name)
        if (current()) this.tree = data
      } catch (err) {
        if (!current()) return
        console.warn("Failed to load session tree:", err)
        this.tree = null
      } finally {
        if (current()) this.loadingTree = false
      }
    },

    loadSummary(agent = null) {
      if (!this.name) return Promise.resolve()
      const promise = this.fetchSummary(agent)
      if (agent == null) {
        const request = { name: this.name, epoch: this.loadEpoch, promise }
        summaryRequests.set(this, request)
        promise.finally(() => {
          if (summaryRequests.get(this) === request) summaryRequests.delete(this)
        })
      }
      return promise
    },

    async fetchSummary(agent) {
      if (!this.name) return
      const name = this.name
      const current = requestIdentity(this, "summaryRequest")
      const discoveryCurrent =
        agent == null ? requestIdentity(this, "discoveryRequest") : () => false
      if (agent == null) {
        this.summaryStatus = "pending"
        this.summaryAgents = []
        this.summaryError = ""
      }
      this.loadingSummary = true
      try {
        const data = await sessionAPI.getSummary(name, agent)
        if (current()) this.summary = data
        if (discoveryCurrent()) {
          this.unfilteredSummary = data
          this.summaryAgents = agentNames(data?.agents || [])
          this.summaryStatus = "success"
        }
      } catch (err) {
        if (current()) {
          console.warn("Failed to load session summary:", err)
          this.summary = null
        }
        if (discoveryCurrent()) {
          this.summaryStatus = err?.response?.status === 404 ? "not-found" : "error"
          this.summaryError =
            this.summaryStatus === "error"
              ? `Failed to load session summary: ${err.message || err}`
              : ""
        }
      } finally {
        if (current()) this.loadingSummary = false
      }
    },
  },
}

const _sessionDetailFactories = new Map()

function _factoryFor(scope) {
  const key = scope || "default"
  let useFn = _sessionDetailFactories.get(key)
  if (!useFn) {
    useFn = defineStore(`sessionDetail:${key}`, _sessionDetailOptions)
    _sessionDetailFactories.set(key, useFn)
    if (scope) {
      registerScopeDisposer(scope, (pinia) => {
        try {
          const store = useFn(pinia)
          store.loadEpoch++
          summaryRequests.delete(store)
          disposeScopeStore(store, pinia)
        } catch {
          /* swallow */
        }
        _sessionDetailFactories.delete(key)
      })
    }
  }
  return useFn
}

export function useSessionDetailStore(scope) {
  if (scope !== undefined) return _factoryFor(scope)()
  if (getCurrentInstance()) return _factoryFor(injectScope())()
  return _factoryFor(null)()
}
