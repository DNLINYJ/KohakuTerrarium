import { createPinia, setActivePinia } from "pinia"
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import { terrariumAPI } from "@/utils/api"
import { useChatStore } from "./chat"

beforeEach(() => setActivePinia(createPinia()))
afterEach(() => vi.restoreAllMocks())

describe("/btw command routing", () => {
  it.each([false, true])(
    "preserves parent processing=%s without websocket input or optimistic rows",
    async (busy) => {
      const chat = useChatStore()
      chat._instanceId = "session-a"
      chat._instanceGraphId = "graph-a"
      chat.tabs = ["root"]
      chat.activeTab = "root"
      chat.messagesByTab.root = [{ role: "assistant", content: "parent reply", _streaming: busy }]
      chat.processingByTab.root = busy
      chat.queuedMessagesByTab.root = []
      const result = { data: { type: "side_conversation", job_id: "job-a", name: "fork", run: 0 } }
      const command = vi.spyOn(terrariumAPI, "executeCreatureCommand").mockResolvedValue(result)
      const inventory = vi.spyOn(chat, "loadCommandInventory")
      chat.markSlashTarget("root", { type: "skill", name: "btw" })
      expect(
        await chat.prepareSlashSend({ key: "root", type: "creature" }, "/btw question"),
      ).toEqual({ type: "command", name: "btw" })
      expect(inventory).not.toHaveBeenCalled()
      for (const args of [
        "question",
        "--continue job-a follow up",
        "--stop job-a",
        "--open job-a",
      ]) {
        expect(await chat.send(`/btw ${args}`)).toEqual({ handled: "command", result })
        expect(command).toHaveBeenLastCalledWith("graph-a", "root", "btw", args)
      }
      chat._onMessage({
        type: "activity",
        activity_type: "btw_result",
        source: "root",
        detail: "private side answer",
        audience: "user",
        job_id: "job-a",
      })
      expect(chat.messagesByTab.root).toEqual([
        { role: "assistant", content: "parent reply", _streaming: busy },
      ])
      expect(chat.processingByTab.root).toBe(busy)
      expect(chat.queuedMessagesByTab.root).toEqual([])
      expect(chat._ws).toBeNull()
    },
  )

  it("does not fall back to parent input after a command failure", async () => {
    const chat = useChatStore()
    chat.activeTab = "root"
    chat.processingByTab.root = true
    vi.spyOn(terrariumAPI, "executeCreatureCommand").mockRejectedValue(new Error("unavailable"))
    await expect(chat.send("/btw question")).rejects.toThrow("unavailable")
    expect(chat.messagesByTab.root).toBeUndefined()
    expect(chat.processingByTab.root).toBe(true)
  })
})
