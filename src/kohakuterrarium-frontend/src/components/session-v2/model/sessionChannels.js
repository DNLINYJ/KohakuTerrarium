import { isDirectChannel } from "@/utils/graph/data/model"

/**
 * A session's channels as people see them. A creature's direct channel (the
 * message alias a recipe creates under its name) is not a channel anyone
 * designed, so it is never listed.
 */
export function visibleChannels(instance) {
  const names = new Set((instance?.creatures || []).map((c) => c.name))
  return (instance?.channels || []).filter((ch) => !isDirectChannel(ch, names))
}
