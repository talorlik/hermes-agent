import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

type RegisteredPage = React.ComponentType

const bundlePath = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  '../../../../../plugins/kanban/dashboard/dist/index.js',
)

const bundle = fs.readFileSync(bundlePath, 'utf8')

function primitive(tag: string) {
  return function Primitive({ children, ...props }: Record<string, unknown>) {
    const allowed = Object.fromEntries(
      Object.entries(props).filter(([key]) =>
        ['className', 'disabled', 'id', 'onChange', 'onClick', 'onKeyDown', 'placeholder', 'title', 'type', 'value'].includes(key),
      ),
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
      SelectOption: primitive('option'),
    },
    hooks: {
      useCallback: React.useCallback,
      useEffect: React.useEffect,
      useMemo: React.useMemo,
      useRef: React.useRef,
      useState: React.useState,
    },
    utils: {
      cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
      timeAgo: () => 'now',
    },
    fetchJSON,
    authedFetch: vi.fn(),
    buildWsUrl: vi.fn().mockResolvedValue('ws://example.invalid/events'),
  }

  Object.assign(window, {
    __HERMES_PLUGIN_SDK__: sdk,
    __HERMES_PLUGINS__: {
      register: (_slug: string, component: RegisteredPage) => {
        registered = component
      },
    },
    WebSocket: class {
      close() {}
    },
  })
  window.eval(bundle)

  if (registered === null) {throw new Error('Kanban dashboard did not register')}

  return registered
}

function createFetch(options: { unknownTask?: boolean } = {}) {
  return vi.fn(async (url: string) => {
    if (url.includes('/config')) {return { render_markdown: true }}

    if (url.includes('/boards')) {
      return {
        current: 'default',
        boards: [
          { slug: 'default', name: 'Default', counts: {} },
          { slug: 'ops board', name: 'Ops Board', counts: {} },
          { slug: 'saved-board', name: 'Saved Board', counts: {} },
        ],
      }
    }

    if (url.includes('/home-channels')) {return { home_channels: [] }}

    if (url.includes('/tasks/')) {
      if (options.unknownTask) {throw new Error('404: {"detail":"task not found"}')}

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
                created_at: 1,
              },
            ],
          },
        ],
      }
    }

    return {}
  })
}

function requested(fetchJSON: ReturnType<typeof vi.fn>, fragment: string) {
  return fetchJSON.mock.calls.map(([url]) => String(url)).filter((url) => url.includes(fragment))
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
      expect(requested(fetchJSON, '/board').some((url) => url.includes('board=ops%20board'))).toBe(true)
      expect(requested(fetchJSON, '/tasks/t_abc%2F123').some((url) => url.includes('board=ops%20board'))).toBe(true)
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
    expect(requested(fetchJSON, '/board').some((url) => url.includes('board=ops%20board'))).toBe(true)
  })

  it('preserves the saved board when URL parameters are absent', async () => {
    window.localStorage.setItem('hermes.kanban.selectedBoard', 'saved-board')
    window.history.replaceState({}, '', '/kanban')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Board remains usable')
    expect(screen.queryByTitle('Close (Esc)')).toBeNull()
    expect(requested(fetchJSON, '/board').some((url) => url.includes('board=saved-board'))).toBe(true)
  })

  it('preserves the saved board when URL parameters are blank', async () => {
    window.localStorage.setItem('hermes.kanban.selectedBoard', 'saved-board')
    window.history.replaceState({}, '', '/kanban?board=%20&task=%20')
    const fetchJSON = createFetch()
    const Page = loadDashboard(fetchJSON)

    render(<Page />)

    await screen.findByText('Board remains usable')
    expect(screen.queryByTitle('Close (Esc)')).toBeNull()
    expect(requested(fetchJSON, '/board').some((url) => url.includes('board=saved-board'))).toBe(true)
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
