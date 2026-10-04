import type { Chunk } from './html'

export type NodeMap = WeakMap<Node, Node>
export type HydrationHook = (map: NodeMap, visited: WeakSet<Chunk>) => void

export interface HydrationCapture {
  hooks: WeakMap<Chunk, HydrationHook[]>
}

type HydrationCaptureProvider = () => HydrationCapture | null

let hydrationCaptureProvider: HydrationCaptureProvider | null = null

export function installHydrationCaptureProvider(
  provider: HydrationCaptureProvider | null
) {
  hydrationCaptureProvider = provider
}

export function createHydrationCapture(): HydrationCapture {
  return {
    hooks: new WeakMap(),
  }
}

export function getHydrationCapture(): HydrationCapture | null {
  return hydrationCaptureProvider?.() ?? null
}

export function registerHydrationHook(chunk: Chunk, hook: HydrationHook) {
  const capture = getHydrationCapture()
  if (!capture) return
  const hooks = capture.hooks.get(chunk)
  if (hooks) {
    hooks.push(hook)
  } else {
    capture.hooks.set(chunk, [hook])
  }
}

export function adoptCapturedChunk(
  capture: HydrationCapture,
  chunk: Chunk,
  map: NodeMap,
  visited = new WeakSet<Chunk>()
) {
  if (visited.has(chunk)) return
  visited.add(chunk)
  const ref = chunk.ref
  if (ref.f) ref.f = (map.get(ref.f) as ChildNode | undefined) ?? ref.f
  if (ref.l) ref.l = (map.get(ref.l) as ChildNode | undefined) ?? ref.l
  capture.hooks.get(chunk)?.forEach((hook) => hook(map, visited))
}
