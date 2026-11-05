import { describe, expect, it } from "vitest"

import { snapshotContent } from "./contentSnapshot.js"

function expectDetached(source, snapshot) {
  if (source === null || typeof source !== "object") {
    expect(snapshot).toBe(source)
    return
  }
  expect(snapshot).not.toBe(source)
  expect(Object.keys(snapshot)).toEqual(Object.keys(source))
  for (const key of Object.keys(source)) expectDetached(source[key], snapshot[key])
}

describe("JSON content snapshots", () => {
  it("detaches all containers including unknown nested extensions and keeps primitive leaves", () => {
    const source = [
      { type: "vendor", extra: [{ nested: [null, false, 0, "", "A".repeat(1024 * 1024)] }] },
    ]
    const snapshot = snapshotContent(source)
    expectDetached(source, snapshot)
    source[0].extra[0].nested.push("source")
    snapshot[0].extra[0].nested[0] = "snapshot"
    expect(snapshot[0].extra[0].nested).toHaveLength(5)
    expect(source[0].extra[0].nested[0]).toBeNull()
  })

  it("preserves own prototype-like keys as detached data, not setters or inherited properties", () => {
    const source = JSON.parse(
      '{"__proto__":{"polluted":"original"},"constructor":{"prototype":[{"value":1}]},"prototype":{"value":2}}',
    )
    const snapshot = snapshotContent(source)
    expectDetached(source, snapshot)
    expect(Object.getPrototypeOf(snapshot)).toBe(Object.prototype)
    expect(Object.hasOwn(snapshot, "__proto__")).toBe(true)
    snapshot.__proto__.polluted = "changed"
    snapshot.constructor.prototype[0].value = 3
    expect(source.__proto__.polluted).toBe("original")
    expect(source.constructor.prototype[0].value).toBe(1)
    expect({}.polluted).toBeUndefined()
  })

  it.each(["caption", "", null, true, false, 0, 3.5])("retains scalar %j", (value) => {
    expect(snapshotContent(value)).toBe(value)
  })
})
