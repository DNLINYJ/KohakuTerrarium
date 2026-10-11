import { describe, expect, it } from "vitest"

import { visibleChannels } from "./sessionChannels"

describe("visibleChannels", () => {
  it("drops each creature's direct-message alias and keeps designed channels in order", () => {
    const instance = {
      creatures: [{ name: "planner" }, { name: "critic" }],
      channels: [{ name: "tasks" }, { name: "planner" }, { name: "team_chat" }, { name: "critic" }],
    }
    expect(visibleChannels(instance).map((c) => c.name)).toEqual(["tasks", "team_chat"])
  })

  it("trusts the server's direct flag over a shared name", () => {
    const instance = {
      creatures: [{ name: "deploy" }, { name: "opus" }],
      channels: [
        { name: "deploy", direct: false, description: "deployment planning" },
        { name: "opus", direct: true },
        { name: "leads", direct: false },
      ],
    }
    expect(visibleChannels(instance).map((c) => c.name)).toEqual(["deploy", "leads"])
  })

  it("handles a missing instance or lists", () => {
    expect(visibleChannels(null)).toEqual([])
    expect(visibleChannels({ channels: [{ name: "a" }] }).map((c) => c.name)).toEqual(["a"])
  })
})
