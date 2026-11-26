// brunna's surface adapter for Claude Code (docs/design/agent-surface.md, "Adapter: a Claude Code mod").
//
// Display only. It runs one read-only program, `brunna`, for two answers: `brunna surface --json` (this session's view,
// contract session-surface.v1) once a minute, and `brunna glance --json` (glance.v1) when its pane opens or is
// refreshed. It draws what they say and composes no wording of its own, except the `brunna upgrade` command it shows for a
// newer release (shown and copied, never run) and the one line for a contract it doesn't
// know. It puts nothing into the model's context: no prompt.submit hook, no tool, no agent, and its two commands return
// no text. A press or a command only fills the prompt box with the platform's words, for the person to send; nothing is
// ever submitted, approved or run for them. With this mod gone, broken or disabled, nothing brunna guarantees changes.
import { atom, read, update } from 'claude-code'
import type { EngineInterface, Register } from 'claude-code'

import type { BrunnaCommand, BrunnaItem, BrunnaNeeds, BrunnaSurface, BrunnaView } from '../types/index'

const PANE = 'brunna-needs-you'
const POLL_MS = 60_000
const RUN_MS = 20_000
// The contract majors this mod reads; on any other it draws MISMATCH and nothing else.
const SURFACE_V = 1
const GLANCE_V = 1
// What the person types for a newer brunna release (labels.update says which is out); shown and copied, never run.
const UPGRADE = 'brunna upgrade'
const MISMATCH = "brunna and its Claude Code mod don't match"
// The commands it may register: the platform lists what it offers, this mod registers only these names.
const ALLOWED = ['inbox', 'brunna'] as const
// Variables passed blank (a child's variables can only be set, and the platform reads an empty one as unset): a
// project's settings `env` reaches this process's children (measured on 2.1.327), so the `brunna` found must not take
// its instance, root or Python from there, nor load code through Python's or the loader's own variables.
// PLATFORM_VARS: every variable the platform reads (bin/names.py: AGENT_HUB_<X>, AGENTHUB_<X>, BRUNNA_<X>).
const PLATFORM_VARS = ['ROOT', 'PYTHON', 'HOME', 'CONFIG', 'DATA', 'SECRETS', 'STATE', 'EVALS', 'LANG_UI', 'LANG_CONTENT',
  'PROJECT', 'CONFINED', 'JOB', 'JOB_CHAIN', 'JOB_DEPTH', 'EVENTS', 'TIER_CACHE', 'CONNECT_ROOTS', 'GIT_BIN',
  'GIT_SOCKET', 'MCP_ASKER', 'MEMLOCK_WAIT', 'OFFLINE', 'PRIVATE_NAMES', 'RESTORE_CRASH_AFTER', 'RUN_LOGIN', 'TEST_CALLER', 'TEST_GUARD', 'TEST_RUNNER',
  'TEST_WRAPPED']
const OTHER_VARS = ['HUB_LLM_POINT', 'HUBBUS_LEASE', 'HUB_AS_PROJECT', 'HUB_MEMORY_OF', 'HUB_THROWAWAY_COPY',
  'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONINSPECT', 'PYTHONUSERBASE', 'PYTHONPLATLIBDIR',
  'PYTHONEXECUTABLE', 'PYTHONBREAKPOINT', 'PYTHONWARNINGS', 'PYTHONPYCACHEPREFIX', 'PYTHONIOENCODING', 'PYTHONUTF8',
  'PYTHONOPTIMIZE', 'PYTHONDEBUG', 'PYTHONVERBOSE', 'PYTHONMALLOC', 'PYTHONDEVMODE', 'PYTHONSAFEPATH',
  'PYTHONNOUSERSITE', 'PYTHONPROFILEIMPORTTIME', 'PYTHONTRACEMALLOC', 'PYTHON_FROZEN_MODULES', 'PYTHON_GIL',
  'BASH_ENV', 'ENV', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH', 'DYLD_FRAMEWORK_PATH', 'LD_PRELOAD',
  'LD_LIBRARY_PATH']
const BLANK: Record<string, string> = Object.fromEntries([
  ...PLATFORM_VARS.flatMap(x => ['AGENT_HUB_', 'AGENTHUB_', 'BRUNNA_'].map(p => p + x)),
  ...OTHER_VARS,
].map(name => [name, '']))
// A command element the pane shows and copies: a plain word, so what is pasted is what is shown.
const PLAIN = /^[A-Za-z0-9._\/:=-]+$/

type Command = BrunnaCommand
type Surface = BrunnaSurface
type View = BrunnaView
type Item = BrunnaItem
type Needs = BrunnaNeeds

const view = atom({ plugin: 'brunna', key: 'view' } as const, null, { shape: 'view-1' })
const needs = atom({ plugin: 'brunna', key: 'needs' } as const, null, { shape: 'needs-1' })

const isObj = (x: unknown): x is Record<string, unknown> => typeof x === 'object' && x !== null && !Array.isArray(x)
const str = (x: unknown): string => (typeof x === 'string' ? x : '')
const strs = (x: unknown): string[] => (Array.isArray(x) ? x.filter((s): s is string => typeof s === 'string') : [])

type Engine = EngineInterface

// `brunna <args>` from a neutral folder (the user's home), found at install-cli's place first, then on PATH. Its JSON,
// or undefined when it can't be run or answers nothing usable.
async function brunna($: Engine, args: string[]): Promise<unknown> {
  const home = await $.env.get('HOME')
  const init = { cwd: home || '/', env: BLANK, timeoutMs: RUN_MS }
  for (const exe of [home ? `${home}/.local/bin/brunna` : '', 'brunna']) {
    if (!exe) continue
    let ran
    try {
      ran = await $.process.run([exe, ...args], init)
    } catch {
      continue // not there, or it could not start: the next place
    }
    if (ran.exitCode !== 0) return undefined
    try {
      return JSON.parse(ran.stdout)
    } catch {
      return undefined
    }
  }
  return undefined
}

function surfaceOf(out: Record<string, unknown>): Surface | null {
  const served = out.served === true
  if (out.set_up !== true || !(served || out.why_not === 'gated')) return null
  const owner = isObj(out.owner) ? out.owner : {}
  const labels: Record<string, string> = {}
  if (isObj(out.labels)) for (const [k, v] of Object.entries(out.labels)) if (typeof v === 'string') labels[k] = v
  const commands: Command[] = []
  for (const c of Array.isArray(out.commands) ? out.commands : []) {
    if (!isObj(c)) continue
    const name = ALLOWED.find(n => n === c.name)
    if (name) commands.push({ name, summary: str(c.summary), ...(typeof c.prompt === 'string' ? { prompt: c.prompt } : {}) })
  }
  return {
    project: str(out.project),
    served,
    lines: strs(out.lines),
    labels,
    owner: {
      needs_you: typeof owner.needs_you === 'number' ? owner.needs_you : 0,
      side: str(owner.side),
      health: str(owner.health),
    },
    commands,
    update: isObj(out.update),
    quiet: out.quiet === true,
  }
}

async function poll($: Engine, registered: Set<string>): Promise<void> {
  const [cwd, id] = await Promise.all([$.session.cwd(), $.session.id()]) // read each time: a /clear changes the id
  const out = await brunna($, ['surface', '--json', '--runtime', 'claude', '--session', id, '--cwd', cwd])
  if (!isObj(out)) return // it couldn't run or answered nothing readable: the last view stays
  let next: View = null
  if (out.v !== SURFACE_V) next = { kind: 'mismatch' }
  else {
    const s = surfaceOf(out)
    next = s ? { kind: 'surface', surface: s } : null
  }
  await update($, view, () => next)
  if (next?.kind !== 'surface') return
  for (const c of next.surface.commands) {
    if (registered.has(c.name) || !c.summary) continue
    if (c.name === 'inbox') await $.command.register({ name: 'inbox', description: c.summary })
    if (c.name === 'brunna') await $.command.register({ name: 'brunna', description: c.summary })
    registered.add(c.name) // only once it took: a refused registration is tried again at the next poll
  }
}

async function loadNeeds($: Engine): Promise<void> {
  const out = await brunna($, ['glance', '--json'])
  let next: Needs = { kind: 'unreadable' }
  if (isObj(out)) {
    if (out.v !== GLANCE_V) next = { kind: 'mismatch' }
    else if (out.set_up === true) {
      const items: Item[] = []
      for (const x of Array.isArray(out.needs_you) ? out.needs_you : []) {
        if (!isObj(x)) continue
        const command = strs(x.command)
        items.push({
          kind: str(x.kind),
          project: str(x.project),
          id: str(x.id),
          text: str(x.text),
          since: str(x.since),
          ...(typeof x.prompt === 'string' ? { prompt: x.prompt } : {}),
          ...(command.length >= 2 && command[0] === 'brunna' && command.every(w => PLAIN.test(w)) ? { command } : {}),
        })
      }
      items.sort((a, b) => (a.since < b.since ? 1 : a.since > b.since ? -1 : 0)) // newest first
      const health = isObj(out.health) ? out.health : {}
      const alerts = (Array.isArray(health.alerts) ? health.alerts : []).map(a => (isObj(a) ? str(a.text) : '')).filter(Boolean)
      next = { kind: 'items', items, alerts, update: isObj(out.update) }
    }
  }
  await update($, needs, () => next)
}

async function openPane($: Engine): Promise<void> {
  const v = await read($, view)
  const title = v?.kind === 'surface' ? v.surface.labels.title : undefined
  await $.ui.open({ id: PANE, ...(title ? { title } : {}) })
  await loadNeeds($)
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    // only where the session draws somewhere: a `-p` run draws nowhere (isInteractive is false in an SDK host that
    // still draws, so it isn't asked)
    if ((await $.session.surfaces()).length > 0) {
      const registered = new Set<string>()
      const tick = () => void poll($, registered).catch(() => undefined) // a failed poll leaves the last view
      tick()
      $.clock.every(POLL_MS, tick)
    }
    return next(e)
  })

  // The two commands answer for themselves and return no text: a plugin command's output reaches the model. They
  // catch their own failures, so a hook skipped for throwing never leaves the command to answer some other way.
  on('command.run', { command: 'inbox' }, async $ => {
    try {
      const v = await read($, view)
      const prompt = v?.kind === 'surface' ? v.surface.commands.find(c => c.name === 'inbox')?.prompt : undefined
      if (prompt) await $.prompt.fill({ text: prompt })
    } catch {
      // nothing filled: the person types the request themselves
    }
    return {}
  })

  on('command.run', { command: 'brunna' }, async $ => {
    try {
      await openPane($)
    } catch {
      // no pane: `brunna glance` shows the same
    }
    return {}
  })

  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    if (e.props.hasSurvey) return next(e)
    const v = await read($, view)
    const { Box, Button, Text } = $.ui.resolve(e)
    if (v?.kind === 'mismatch') return <Text dimColor>{MISMATCH}</Text>
    if (v?.kind !== 'surface' || v.surface.lines.length === 0) return next(e)
    const s = v.surface
    const open = s.labels.open
    const wants = s.owner.needs_you > 0 || s.owner.health === 'problem' || s.update
    return (
      <Box flexDirection="row" gap={1}>
        <Text wrap="truncate-end" dimColor={s.quiet}>{s.lines.join('  ·  ')}</Text>
        {wants && open ? <Button key="open" label={open} onPress={() => void openPane($).catch(() => undefined)} /> : null}
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Button, Text } = $.ui.resolve(e)
    const v = await read($, view)
    const n = await read($, needs)
    if (v?.kind === 'mismatch' || n?.kind === 'mismatch') return <Text>{MISMATCH}</Text>
    const s = v?.kind === 'surface' ? v.surface : null
    const L = s?.labels ?? {}
    // settling belongs to the assistant's side when it is the owner side: the whole pane is read-only then
    const readOnly = s?.owner.side === 'assistant'
    const rows = n?.kind === 'items' ? n.items : []
    return (
      <Box flexDirection="column">
        {L.refresh ? <Button key="refresh" label={L.refresh} onPress={() => void loadNeeds($).catch(() => undefined)} /> : null}
        {readOnly && L.read_only ? <Text dimColor>{L.read_only}</Text> : null}
        {n?.kind === 'unreadable' && L.unreadable ? <Text dimColor>{L.unreadable}</Text> : null}
        {n?.kind === 'items' && n.update && L.update ? (
          <Box flexDirection="column">
            <Text>{L.update}</Text>
            <Box flexDirection="row" gap={1}>
              <Text dimColor>{UPGRADE}</Text>
              {L.copy ? (
                <Button key="copy:update" label={L.copy} onPress={p => void $.ui.copy({ text: UPGRADE, surface: p.surface }).catch(() => undefined)} />
              ) : null}
            </Box>
          </Box>
        ) : null}
        {n?.kind === 'items' && rows.length === 0 && L.empty ? <Text dimColor>{L.empty}</Text> : null}
        {rows.map(item => {
          const key = `${item.project}/${item.id}`
          const kind = (item.kind === 'question' ? L.question : L.todo) ?? item.kind
          const command = item.command ? item.command.join(' ') : ''
          // a press fills the prompt only for this session's own project, with the platform's words (never the text)
          const canAsk = !readOnly && !command && !!item.prompt && !!s && item.project === s.project && !!L.ask
          return (
            <Box key={key} flexDirection="column" marginTop={1}>
              <Text>{`${kind} · ${item.project} · ${item.text}`}</Text>
              {command ? (
                <Box flexDirection="row" gap={1}>
                  <Text dimColor>{command}</Text>
                  {L.copy ? (
                    <Button key={`copy:${key}`} label={L.copy} onPress={p => void $.ui.copy({ text: command, surface: p.surface }).catch(() => undefined)} />
                  ) : null}
                </Box>
              ) : null}
              {canAsk ? (
                <Button key={`ask:${key}`} label={L.ask} onPress={() => void $.prompt.fill({ text: item.prompt ?? '', mode: 'append' }).catch(() => undefined)} />
              ) : null}
            </Box>
          )
        })}
        {n?.kind === 'items' && n.alerts.length > 0 ? (
          <Box flexDirection="column" marginTop={1}>
            {L.health ? <Text bold>{L.health}</Text> : null}
            {n.alerts.map(a => (
              <Text>{a}</Text>
            ))}
          </Box>
        ) : null}
      </Box>
    )
  })
}
