// The mod's state, as the engine holds it for the session (hooks/brunna.tsx). Read from brunna's two answers only:
// `brunna surface --json` (session-surface.v1) and `brunna glance --json` (glance.v1).

export type BrunnaCommand = { name: 'inbox' | 'brunna'; summary: string; prompt?: string }

export type BrunnaSurface = {
  project: string
  served: boolean
  lines: string[]
  labels: Record<string, string>
  owner: { needs_you: number; side: string; health: string }
  commands: BrunnaCommand[]
  update: boolean // a newer brunna release is out: labels.update says which
  quiet: boolean // the only line is that release's
}

export type BrunnaView = { kind: 'surface'; surface: BrunnaSurface } | { kind: 'mismatch' } | null

export type BrunnaItem = {
  kind: string
  project: string
  id: string
  text: string
  since: string
  prompt?: string
  command?: string[]
}

export type BrunnaNeeds =
  | { kind: 'items'; items: BrunnaItem[]; alerts: string[]; update: boolean }
  | { kind: 'mismatch' }
  | { kind: 'unreadable' }
  | null

declare module 'claude-code' {
  interface PluginState {
    brunna: { view: Shaped<BrunnaView>; needs: Shaped<BrunnaNeeds> }
  }
}
