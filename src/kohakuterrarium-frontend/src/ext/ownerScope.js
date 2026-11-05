/**
 * The runtime scope (session + focused creature) a contribution is mounted in.
 *
 * Matches the module panels' routing: the session is the instance's
 * ``graph_id`` (or id); the creature is the focused chat tab, or the only
 * creature of a solo session. Outside a session both are empty.
 */

import { computed } from "vue"

import { injectScope } from "@/composables/useScope"
import { useChatStore } from "@/stores/chat"
import { useInstancesStore } from "@/stores/instances"

export function useOwnerScope() {
  const scope = injectScope()
  if (!scope) return computed(() => ({ sessionId: "", creatureId: "" }))
  const instances = useInstancesStore()
  const chat = useChatStore(scope)
  return computed(() => {
    const match = (it) => it && (it.id === scope || it.graph_id === scope)
    const instance = match(instances.current)
      ? instances.current
      : instances.list.find(match) || null
    const creatures = instance?.creatures || []
    const creatureId = chat.terrariumTarget || (creatures.length === 1 ? creatures[0].name : "")
    return { sessionId: instance?.graph_id || instance?.id || scope, creatureId: creatureId || "" }
  })
}
