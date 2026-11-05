/**
 * The one multiplexed push socket (``/ws/extensions/owner``) per host.
 *
 * Every owner subscription on the page shares it. Subscriptions are
 * re-sent after a reconnect; the socket closes when the last one ends
 * or the host changes.
 */

import { wsUrl } from "@/utils/wsUrl"

const PATH = "/ws/extensions/owner"
const MAX_BACKOFF_MS = 15000

let _factory = (url) => new WebSocket(url)
let _socket = null
let _seq = 0
let _attempt = 0
let _timer = null
const _subs = new Map()

/** Replace the WebSocket constructor; for tests. */
export function setOwnerSocketFactory(factory) {
  _factory = factory || ((url) => new WebSocket(url))
}

function send(message) {
  if (_socket && _socket.readyState === 1) _socket.send(JSON.stringify(message))
}

function subscribeMessage(id, params) {
  return {
    op: "subscribe",
    id,
    owner: params.owner,
    session_id: params.sessionId || "",
    creature_id: params.creatureId || "",
  }
}

function connect() {
  if (_socket || !_subs.size) return
  const socket = _factory(wsUrl(PATH))
  _socket = socket
  socket.onopen = () => {
    _attempt = 0
    for (const [id, sub] of _subs) send(subscribeMessage(id, sub.params))
  }
  socket.onmessage = (ev) => {
    let message
    try {
      message = JSON.parse(ev.data)
    } catch (_err) {
      return
    }
    if (message?.type === "overflow") {
      // The server dropped pushes; every subscriber re-reads its state.
      for (const sub of _subs.values()) sub.onMessage(message)
      return
    }
    const sub = _subs.get(message?.id)
    if (sub) sub.onMessage(message)
  }
  socket.onclose = () => {
    if (_socket !== socket) return
    _socket = null
    if (!_subs.size) return
    const delay = Math.min(MAX_BACKOFF_MS, 500 * 2 ** _attempt++)
    _timer = setTimeout(connect, delay)
  }
}

/**
 * Subscribe to an owner's pushes. ``onMessage`` receives server frames
 * (``subscribed`` / ``event`` / ``error``). Returns an unsubscribe function.
 */
export function subscribeOwner(params, onMessage) {
  const id = `s${++_seq}`
  _subs.set(id, { params, onMessage })
  if (_socket) send(subscribeMessage(id, params))
  else connect()
  return () => {
    if (!_subs.delete(id)) return
    send({ op: "unsubscribe", id })
    if (!_subs.size) closeOwnerSocket()
  }
}

/** Close the socket and forget every subscription (host switch). */
export function closeOwnerSocket({ forget = false } = {}) {
  if (forget) _subs.clear()
  clearTimeout(_timer)
  _timer = null
  const socket = _socket
  _socket = null
  if (socket) socket.close()
}

export function ownerSubscriptionCount() {
  return _subs.size
}
