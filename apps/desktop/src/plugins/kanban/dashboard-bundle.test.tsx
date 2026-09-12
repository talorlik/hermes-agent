import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type RegisteredPage = React.ComponentType

const bundlePath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../../../../plugins/kanban/dashboard/dist/index.js'
)

const bundle = fs.readFileSync(bundlePath, 'utf8')

function primitive(tag: string) {
  return function Primitive({ children, ...props }: Record<string, unknown>) {
    const allowed = Object.fromEntries(
      Object.entries(props).filter(([key]) =>
        [
          'className',
          'disabled',
          'id',
          'onChange',
          'onClick',
          'onKeyDown',
          'placeholder',
          'title',
          'type',
          'value'
        ].includes(key)
      )
    )

    return React.createElement(tag, allowed, children as React.ReactNode)
  }
}

function loadDashboard(fetchJSON: ReturnType<typeof vi.fn>): RegisteredPage {
  let registered: RegisteredPage | null = null

  const sdk = {
    React,
    components: {
      Badge: primitive('span'),
      Button: primitive('button'),
      Card: primitive('div'),
      CardContent: primitive('div'),
      ConfirmDialog: () => null,
      Input: primitive('input'),
      Label: primitive('label'),
      Select: primitive('select'),
      SelectOption: primitive('option')
    },
    hooks: {
      useCallback: React.useCallback,
      useEffect: React.useEffect,
      useMemo: React.useMemo,
      useRef: React.useRef,
      useState: React.useState
    },
    utils: {
      cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
      timeAgo: () => 'now'
    },
    fetchJSON,
    authedFetch: vi.fn(),
    buildWsUrl: vi.fn().mockResolvedValue('ws://example.invalid/events')
  }

  Object.assign(window, {
    __HERMES_PLUGIN_SDK__: sdk,
    __HERMES_PLUGINS__: {
      register: (_slug: string, component: RegisteredPage) => {
        registered = component
      }
    },
    WebSocket: class {
      close() {}
    }
  })
  window.eval(bundle)

  if (registered === null) {
    throw new Error('Kanban dashboard did not register')
  }

  return registered
}

function createFetch(
  options: {
    unknownTask?: boolean
    summary?: Record<string, unknown> | ((url: string) => Record<string, unknown> | Promise<Record<string, unknown>>)
    summaryError?: Error
  } = {}
) {
  return vi.fn(async (url: string) => {
    if (url.includes('/config')) {
      return { render_markdown: true }
    }

    if (url.includes('/boards')) {
      return {
        current: 'default',
        boards: [
          { slug: 'default', name: 'Default', counts: {} },
          { slug: 'ops board', name: 'Ops Board', counts: {} },
          { slug: 'saved-board', name: 'Saved Board', counts: {} }
        ]
      }
    }

    if (url.includes('/home-channels')) {
      return { home_channels: [] }
    }

    if (url.includes('/board-summary')) {
      if (options.summaryError) {
        throw options.summaryError
      }

      if (typeof options.summary === 'function') {
        return options.summary(url)
      }

      if (options.summary) {
        return options.summary
      }

      throw new Error('404: {"detail":"board summary not found"}')
    }

    if (url.includes('/tasks/')) {
      if (options.unknownTask) {
        throw new Error('404: {"detail":"task not found"}')
      }

      return new Promise(() => undefined)
    }

    if (url.includes('/board')) {
      return {
        latest_event_id: 0,
        tenants: [],
        assignees: [],
        columns: [
          {
            name: 'todo',
            tasks: [
              {
                id: 't_board',
                title: 'Board remains usable',
                status: 'todo',
                priority: 1,
                created_at: 1
              }
            ]
          }
        ]
      }
    }

    return {}
  })
}

function requested(fetchJSON: ReturnType<typeof vi.fn>, fragment: string) {
  return fetchJSON.mock.calls.map(([url]) => String(url)).filter(url => url.includes(fragment))
}

describe('shipped Kanban dashboard deep links', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.history.replaceState({}, '', '/kanban')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('selects the encoded URL board and opens the requested task drawer', async () => {
    window.localStorage.setItem('hermes.kanban.selectedBoard', 'saved-board')
    window.history.replaceState({}, '', '/kanban?board=ops%20board&task=t_abc%2F123')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('t_abc/123')
    expect(screen.getByTitle('Close (Esc)')).toBeTruthy()
    await waitFor(() => {
      expect(requested(fetchJSON, '/board').some(url => url.includes('board=ops%20board'))).toBe(true)
      expect(requested(fetchJSON, '/tasks/t_abc%2F123').some(url => url.includes('board=ops%20board'))).toBe(true)
    })
    expect(window.localStorage.getItem('hermes.kanban.selectedBoard')).toBe('saved-board')
  })

  it('keeps the saved board when a deep link names a missing board', async () => {
    window.localStorage.setItem('hermes.kanban.selectedBoard', 'saved-board')
    window.history.replaceState({}, '', '/kanban?board=missing-board')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Board remains usable')
    await waitFor(() => {
      expect(requested(fetchJSON, '/board').some(url => url.includes('board=saved-board'))).toBe(true)
    })
    expect(window.localStorage.getItem('hermes.kanban.selectedBoard')).toBe('saved-board')
  })

  it('uses a board-only link without opening a task drawer', async () => {
    window.history.replaceState({}, '', '/kanban?board=ops%20board')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Board remains usable')
    expect(screen.queryByTitle('Close (Esc)')).toBeNull()
    expect(requested(fetchJSON, '/tasks/')).toHaveLength(0)
    expect(requested(fetchJSON, '/board').some(url => url.includes('board=ops%20board'))).toBe(true)
  })

  it('preserves the saved board when URL parameters are absent', async () => {
    window.localStorage.setItem('hermes.kanban.selectedBoard', 'saved-board')
    window.history.replaceState({}, '', '/kanban')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Board remains usable')
    expect(screen.queryByTitle('Close (Esc)')).toBeNull()
    expect(requested(fetchJSON, '/board').some(url => url.includes('board=saved-board'))).toBe(true)
  })

  it('preserves the saved board when URL parameters are blank', async () => {
    window.localStorage.setItem('hermes.kanban.selectedBoard', 'saved-board')
    window.history.replaceState({}, '', '/kanban?board=%20&task=%20')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Board remains usable')
    expect(screen.queryByTitle('Close (Esc)')).toBeNull()
    expect(requested(fetchJSON, '/board').some(url => url.includes('board=saved-board'))).toBe(true)
  })

  it('contains an unknown-task error in a closable drawer and keeps the board usable', async () => {
    window.history.replaceState({}, '', '/kanban?board=ops%20board&task=t_missing')
    const fetchJSON = createFetch({ unknownTask: true })
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('404: {"detail":"task not found"}')
    expect(screen.getByText('Board remains usable')).toBeTruthy()
    fireEvent.click(screen.getByTitle('Close (Esc)'))
    await waitFor(() => expect(screen.queryByText('t_missing')).toBeNull())
    expect(screen.getByText('Board remains usable')).toBeTruthy()
  })
})

describe('shipped Kanban orchestration summary', () => {
  beforeEach(() => {
    window.localStorage.clear()
    window.history.replaceState({}, '', '/kanban?board=default')
  })

  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders the validated summary without blocking the board', async () => {
    const fetchJSON = createFetch({
      summary: {
        board: 'default',
        generated_at: '2026-09-12T19:00:00Z',
        expires_at: '2026-09-12T19:02:00Z',
        stale: true,
        phase: 'canary',
        status: 'canary_passed',
        completed: false,
        cron_removal_authorized: false,
        counts: {
          running_count: 2,
          failed_count: 1,
          blocked_count: 3,
          findings_count: 1
        },
        cron: { jobs_remaining: 4 },
        lanes: [
          {
            name: 'cc_backups',
            running_count: 1,
            paused_count: 0,
            failed_count: 1,
            terminal_successes: 5,
            contention_count: 0,
            duplicate_count: 0,
            stale_prerequisite_count: 0
          }
        ],
        schedules: [],
        schedule_totals: {
          configured: 9,
          observed: 9,
          paused: 2,
          drifted: 0,
          pending: 0,
          missed: 0
        },
        barrier: {
          passed: true,
          waves: 5,
          inventory_digest: 'a'.repeat(64),
          workflow_digest: 'b'.repeat(64)
        },
        findings_count: 1,
        findings: [{ severity: 'error', code: 'TEST', message: 'Example finding' }]
      }
    })

    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Orchestration summary')
    expect(screen.getByText('Board remains usable')).toBeTruthy()
    expect(screen.getByText('canary_passed')).toBeTruthy()
    expect(screen.getByText('Running 2')).toBeTruthy()
    expect(screen.getByText('Failed 1')).toBeTruthy()
    expect(screen.getByText('Blocked 3')).toBeTruthy()
    expect(screen.getByText('Cron 4')).toBeTruthy()
    expect(screen.getByText('Schedules paused 2/9')).toBeTruthy()
    expect(screen.getByText('Findings 1')).toBeTruthy()
    expect(screen.getByText('cc_backups')).toBeTruthy()
    expect(screen.getByText('Stale')).toBeTruthy()
    expect(screen.getByText('Expired 2026-09-12T19:02:00Z')).toBeTruthy()
    expect(requested(fetchJSON, '/board-summary')).toHaveLength(1)
  })

  it('does not let a stale board response overwrite the selected board summary', async () => {
    let resolveDefault: ((value: Record<string, unknown>) => void) | undefined

    const deferredDefault = new Promise<Record<string, unknown>>(resolve => {
      resolveDefault = resolve
    })

    const summary = (status: string) => ({
      board: 'default',
      generated_at: '2026-09-12T19:00:00Z',
      expires_at: '2026-09-12T19:02:00Z',
      stale: false,
      phase: 'canary',
      status,
      completed: false,
      cron_removal_authorized: false,
      counts: { running_count: 0, failed_count: 0, blocked_count: 0 },
      cron: { jobs_remaining: 0 },
      lanes: [],
      schedules: [],
      schedule_totals: { configured: 1, observed: 1, paused: 0, drifted: 0, pending: 0, missed: 0 },
      barrier: { passed: true, waves: 1, inventory_digest: 'a'.repeat(64), workflow_digest: 'b'.repeat(64) },
      findings_count: 0,
      findings: []
    })

    const fetchJSON = createFetch({
      summary: url => (url.includes('board=default') ? deferredDefault : summary('completed'))
    })

    const Page = loadDashboard(fetchJSON)

    render(<Page />)
    await screen.findByText('Board remains usable')
    fireEvent.change(screen.getByTitle(/Boards are independent work streams/), {
      target: { value: 'ops board' }
    })
    await screen.findByText('completed')
    await act(async () => {
      resolveDefault?.(summary('canary_passed'))
      await deferredDefault
    })

    expect(screen.getByText('completed')).toBeTruthy()
    expect(screen.queryByText('canary_passed')).toBeNull()
  })

  it('contains summary failure in a compact state and keeps the board usable', async () => {
    const fetchJSON = createFetch({
      summaryError: new Error('503: {"detail":"board summary unavailable"}')
    })

    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Orchestration summary unavailable')
    expect(screen.getByText('Board remains usable')).toBeTruthy()
  })
})
