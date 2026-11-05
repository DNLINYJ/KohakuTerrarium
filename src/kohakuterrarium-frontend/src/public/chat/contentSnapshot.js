/** Copy JSON content containers into an independent snapshot; string leaves are immutable. */
export function snapshotContent(value) {
  if (Array.isArray(value)) return value.map(snapshotContent)
  if (value !== null && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, child]) => [key, snapshotContent(child)]),
    )
  }
  return value
}
