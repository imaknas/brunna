// `claude plugin test integrations/claude-code`: the mod against the engine's own test kit. Every hook the test adds
// stands for the engine (nothing sits beneath the plugin), so `brunna` is answered here, never run.
import { expect, mock, test } from 'claude-code/testing'

const SURFACE = {
  v: 1,
  set_up: true,
  served: true,
  why_not: null,
  project: 'garden-app',
  owner: { needs_you: 2, side: 'builtin', health: 'ok' },
  mail: { held: [], waiting: 0 },
  labels: { open: 'needs you', title: 'brunna: needs you', empty: 'Nothing needs you.', ask: 'ask here', copy: 'copy',
    todo: 'to-do', question: 'question', refresh: 'refresh', read_only: 'Shown here only.' },
  lines: ['brunna · 2 need you'],
  commands: [
    { name: 'inbox', summary: "Read and handle this project's letters", prompt: 'Run `brunna inbox`.' },
    { name: 'brunna', summary: 'What needs you' },
    { name: 'deploy', summary: 'not one this mod knows', prompt: 'deploy it' },
  ],
}

const GLANCE = {
  v: 1,
  set_up: true,
  needs_you: [
    { kind: 'todo', project: 'garden-app', id: 'task-1', text: 'Renew the domain', since: '2026-11-18T09:00:00+08:00',
      prompt: 'About my open to-do task-1 in garden-app.' },
    { kind: 'question', project: 'seed-shop', id: 'm1', text: 'Which host?', since: '2026-11-18T08:00:00+08:00',
      prompt: 'Question m1.' },
    { kind: 'todo', project: 'hub', id: 'skills-x', text: 'review skills', since: '2026-11-18T07:00:00+08:00',
      command: ['brunna', 'skills', 'sync', 'garden-app'] },
  ],
  health: { state: 'ok', alerts: [], as_of: null },
}

const BAND = {
  plugin: 'brunna',
  component: 'AbovePrompt',
  props: { hasSurvey: false, isCollapsed: false, isWorking: false, maxRows: 3, bodyColumns: 80,
    scroll: { offset: 0, bodyRows: 3 }, view: {} },
} as const

// a slash command as the person runs it at the prompt
const run = (command: string) => ({ command, args: '', origin: { kind: 'composer' as const },
  presentation: { isFullscreen: false, columns: 100 } })

const PANE = {
  plugin: 'brunna',
  component: 'Pane',
  requestId: 'brunna-needs-you',
  props: { title: 'brunna', isFocused: false, bodyColumns: 60, placement: 'dock', scroll: { offset: 0, bodyRows: 30 },
    view: {} },
} as const

type Seen = { runs: string[][]; cwds: string[]; envs: Record<string, string>[]; filled: string[]; registered: string[] }

function engine(on: any, surface: object = SURFACE, glance: object = GLANCE): Seen {
  const seen: Seen = { runs: [], cwds: [], envs: [], filled: [], registered: [] }
  on('env.get', () => ({ value: '/home/alex' }))
  on('session.surfaces', () => ({ value: ['terminal'] }))
  on('session.cwd', () => ({ value: '/home/alex/workspace/garden-app' }))
  on('session.id', () => ({ value: 'abc' }))
  on('session.start', (_: unknown, e: { cwd: string }) => ({ cwd: e.cwd }))
  on('command.register', (_: unknown, e: { name: string }) => {
    seen.registered.push(e.name)
    return { value: { command: e.name } }
  })
  on('ui.open', () => ({ value: { isPlaced: true } }))
  on('prompt.fill', (_: unknown, e: { text: string }) => {
    seen.filled.push(e.text)
    return { isFilled: true }
  })
  on('process.run', (_: unknown, e: { argv: string[]; init?: { cwd?: string; env?: Record<string, string> } }) => {
    seen.runs.push([...e.argv])
    seen.envs.push({ ...(e.init?.env ?? {}) })
    seen.cwds.push(e.init?.cwd ?? '')
    const out = e.argv[1] === 'surface' ? surface : glance
    return { value: { exitCode: 0, stdout: JSON.stringify(out), stderr: '', isStdoutTruncated: false,
      isStderrTruncated: false } }
  })
  return seen
}

async function started($: any, on: any) {
  const clock = mock.clock(on)
  await $.session.start({ cwd: '/home/alex/workspace/garden-app', surface: 'terminal', isInteractive: true })
  await clock.settle()
}

test('it runs only brunna, from home, and registers only the commands it knows', async ($, on) => {
  const seen = engine(on)
  await started($, on)
  expect(seen.runs[0]).toEqual(['/home/alex/.local/bin/brunna', 'surface', '--json', '--runtime', 'claude', '--session',
    'abc', '--cwd', '/home/alex/workspace/garden-app'])
  expect(seen.cwds[0]).toBe('/home/alex')
  // a project's settings env reaches the child: the platform's and Python's variables go blank
  for (const name of ['PYTHONPATH', 'PYTHONHOME', 'AGENT_HUB_HOME', 'BRUNNA_CONFIG', 'AGENT_HUB_ROOT', 'AGENTHUB_PROJECT',
    'HUB_LLM_POINT']) expect(seen.envs[0]?.[name]).toBe('')
  expect(seen.registered.sort()).toEqual(['brunna', 'inbox'])
})

test('the band shows the lines and opens the pane', async ($, on) => {
  engine(on)
  await started($, on)
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: /2 need you/ }))?.text).toBe('brunna · 2 need you')
    expect(await ui.find({ key: 'open' })).toBeDefined()
    await ui.unmount()
  }
})

test('/inbox fills the platform prompt and returns no text', async ($, on) => {
  const seen = engine(on)
  await started($, on)
  const out = await $.command.run(run('inbox'))
  expect(out.text).toBeUndefined()
  expect(out.context).toBeUndefined()
  expect(seen.filled).toEqual(['Run `brunna inbox`.'])
})

test('the pane: ask only for this project, copy a command, nothing for others', async ($, on) => {
  const seen = engine(on)
  await started($, on)
  const out = await $.command.run(run('brunna'))
  expect(out.text).toBeUndefined()
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...PANE, surface })
    expect(await ui.find({ key: 'ask:garden-app/task-1' })).toBeDefined()
    expect(await ui.find({ key: 'ask:seed-shop/m1' })).toBeUndefined()
    expect(await ui.find({ key: 'copy:hub/skills-x' })).toBeDefined()
    expect(await ui.find({ key: 'ask:hub/skills-x' })).toBeUndefined()
    await ui.press({ key: 'ask:garden-app/task-1' })
    await ui.unmount()
  }
  expect(seen.filled[0]).toBe('About my open to-do task-1 in garden-app.')
})

test('the pane is read-only when the owner side is the assistant', async ($, on) => {
  engine(on, { ...SURFACE, owner: { ...SURFACE.owner, side: 'assistant' } })
  await started($, on)
  await $.command.run(run('brunna'))
  const ui = await $.ui.mount({ ...PANE, surface: 'desktop' })
  expect(await ui.find({ key: 'ask:garden-app/task-1' })).toBeUndefined()
  expect(await ui.find({ type: 'Text', text: /Shown here only/ })).toBeDefined()
  await ui.unmount()
})

const UPDATE = { current: 'v0.22.0', latest: 'v0.23.0', security: false }
const OUT = { ...SURFACE, owner: { ...SURFACE.owner, needs_you: 0 }, lines: ['brunna · v0.23.0 is out'], update: UPDATE,
  quiet: true, labels: { ...SURFACE.labels, update: 'brunna v0.23.0 is out (this machine has v0.22.0).' } }

test('a newer release: a quiet band line that opens the pane', async ($, on) => {
  engine(on, OUT)
  await started($, on)
  for (const surface of ['terminal', 'desktop', 'vscode', 'mobile'] as const) {
    const ui = await $.ui.mount({ ...BAND, surface })
    expect((await ui.find({ type: 'Text', text: /is out/ }))?.text).toBe('brunna · v0.23.0 is out')
    expect(await ui.find({ key: 'open' })).toBeDefined()
    await ui.unmount()
  }
})

test('a newer release: the pane shows the line and the command to copy, and runs nothing', async ($, on) => {
  const seen = engine(on, OUT, { ...GLANCE, update: UPDATE })
  await started($, on)
  await $.command.run(run('brunna'))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ type: 'Text', text: /brunna v0\.23\.0 is out/ })).toBeDefined()
  expect((await ui.find({ type: 'Text', text: /^brunna upgrade$/ }))?.text).toBe('brunna upgrade')
  expect(await ui.find({ key: 'copy:update' })).toBeDefined()
  await ui.press({ key: 'copy:update' })
  await ui.unmount()
  expect(seen.runs.every(r => r[1] === 'surface' || r[1] === 'glance')).toBe(true)
  expect(seen.filled).toEqual([])
})

test('no newer release: the pane says nothing of one', async ($, on) => {
  engine(on)
  await started($, on)
  await $.command.run(run('brunna'))
  const ui = await $.ui.mount({ ...PANE, surface: 'terminal' })
  expect(await ui.find({ key: 'copy:update' })).toBeUndefined()
  await ui.unmount()
})

test('an unknown contract major draws one line', async ($, on) => {
  engine(on, { ...SURFACE, v: 2 })
  await started($, on)
  const ui = await $.ui.mount({ ...BAND, surface: 'terminal' })
  expect((await ui.find({ type: 'Text' }))?.text).toBe("brunna and its Claude Code mod don't match")
  await ui.unmount()
})
