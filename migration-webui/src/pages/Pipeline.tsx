import React, { useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Box, Button, Chip, InputAdornment, Paper, Stack, TextField, Typography,
} from '@mui/material'
import { Search as SearchIcon, OpenInNew as OpenIcon } from '@mui/icons-material'
import { fetchQueue, fetchStages } from '@/api/client'
import type { QueueSnapshot } from '@/api/client'
import { fetchDeadman, fetchFleet, fetchMyMetrics } from '@/api/controlPlane'
import type { DeadmanStatus, FleetNode, MetricsSnapshot } from '@/api/controlPlane'
import type { MigrationStage } from '@/types'
import NodeGraph, { NodeGraphHandle, NodeGraphLegend } from '@/components/NodeGraph'
import { buildGraph, ROLE_COLOR, ROLE_LABEL, traceOf } from '@/pipeline/layout'
import { liveFor } from '@/pipeline/live'
import { EDGES, Role } from '@/pipeline/model'

/**
 * Pipeline -- the whole system: what feeds what, where each part runs, and
 * how far along it is.
 *
 * The wiring is how the system is built and lives in pipeline/model.ts, one
 * description per box, so the picture and its explanations cannot drift. The
 * numbers on the boxes are not static: each comes from a source that really
 * measures it (see pipeline/live.ts), and a box with no source shows none.
 */
const graph = buildGraph()

/** A neighbour in the detail panel; clicking it selects and centres it. */
const NodeLink: React.FC<{ id: string; label: string; onGo: (id: string) => void }> = ({ id, label, onGo }) => (
  <Chip size="small" clickable variant="outlined" label={`${graph.byId.get(id)?.title} · ${label}`}
        onClick={() => onGo(id)} sx={{ mr: 0.5, mb: 0.5, maxWidth: '100%' }} />
)

const settled = <T,>(r: PromiseSettledResult<T>): T | null => r.status === 'fulfilled' ? r.value : null

export const Pipeline: React.FC = () => {
  const navigate = useNavigate()
  const canvas = useRef<NodeGraphHandle>(null)
  const [stages, setStages] = useState<MigrationStage[] | null>(null)
  const [fleet, setFleet] = useState<FleetNode[] | null>(null)
  const [queue, setQueue] = useState<QueueSnapshot | null>(null)
  const [deadman, setDeadman] = useState<DeadmanStatus | null>(null)
  const [metrics, setMetrics] = useState<MetricsSnapshot | null>(null)
  const [err, setErr] = useState('')
  const [selected, setSelected] = useState<string | null>(null)
  const [query, setQuery] = useState('')

  useEffect(() => {
    let alive = true
    let n = 0
    const load = async () => {
      // Metrics are the heavy read; every other source every tick, that one
      // every second tick.
      const [st, fl, q, dm, mt] = await Promise.allSettled([
        fetchStages(), fetchFleet(), fetchQueue(), fetchDeadman(),
        n++ % 2 === 0 ? fetchMyMetrics(120) : Promise.resolve(null),
      ])
      if (!alive) return
      if (st.status === 'fulfilled') { setStages(st.value); setErr('') }
      else setErr(String(st.reason instanceof Error ? st.reason.message : st.reason))
      // Fleet and the dead-man switch are operator-only; a client without
      // them simply sees no line on those boxes.
      setFleet(settled(fl)); setQueue(settled(q)); setDeadman(settled(dm))
      const m = settled(mt)
      if (m) setMetrics(m)
    }
    load()
    const t = window.setInterval(load, 5_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  const live = useMemo(
    () => liveFor({ stages, fleet, metrics, queue, deadman }),
    [stages, fleet, metrics, queue, deadman])
  const nodes = useMemo(
    () => graph.nodes.map((n) => ({ ...n, live: live[n.id] })), [live])

  const q = query.trim().toLowerCase()
  const matches = useMemo(() => {
    if (!q) return null
    return new Set(graph.nodes.filter((n) => {
      const p = graph.byId.get(n.id)!
      return [p.title, p.sub, p.about, ...p.files].some((t) => t.toLowerCase().includes(q))
    }).map((n) => n.id))
  }, [q])

  const trace = useMemo(() => selected ? traceOf(EDGES, selected) : null, [selected])
  const bright = useMemo(() => {
    if (selected && trace) return new Set([selected, ...trace.up, ...trace.down])
    return matches
  }, [selected, trace, matches])

  const go = (id: string) => { setSelected(id); canvas.current?.focus(id) }
  const sel = selected ? graph.byId.get(selected) : undefined
  const feeds = selected ? EDGES.filter((e) => e.from === selected) : []
  const fedBy = selected ? EDGES.filter((e) => e.to === selected) : []
  const liveLine = selected ? live[selected] : undefined

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setSelected(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <Box sx={{ p: 3 }}>
      <Typography variant="h5" sx={{ fontWeight: 700 }}>Pipeline</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 820 }}>
        Everything in the system and how it connects, left to right in the order a migration happens.
        Click a box to see what it is, where it runs, what feeds it and what it touches; click a
        section title to zoom to it. Numbers on the boxes are live — a box with no number has
        nothing measuring it.
      </Typography>
      {err && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Live counts unavailable ({err}) — showing the wiring only.
        </Alert>
      )}

      <Stack direction={{ xs: 'column', lg: 'row' }} spacing={2} alignItems="stretch">
        <Box sx={{ flex: 1, minWidth: 0 }}>
          <TextField id="pipeline-search" size="small" fullWidth sx={{ mb: 1 }}
            placeholder="Find a part — by name, file, or what it does"
            value={query} onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && matches && matches.size) go([...matches][0])
              if (e.key === 'Escape') setQuery('')
            }}
            inputProps={{ 'aria-label': 'Find a part', 'data-testid': 'pipeline-search' }}
            InputProps={{
              startAdornment: <InputAdornment position="start"><SearchIcon fontSize="small" /></InputAdornment>,
              endAdornment: matches ? (
                <InputAdornment position="end">
                  <Typography variant="caption" color="text.secondary" data-testid="pipeline-matches">
                    {matches.size} match{matches.size === 1 ? '' : 'es'}
                  </Typography>
                </InputAdornment>
              ) : undefined,
            }} />
          <Stack direction="row" spacing={0.5} sx={{ mb: 1, flexWrap: 'wrap', rowGap: 0.5, alignItems: 'center' }}
                 data-testid="pipeline-jump">
            <Typography variant="caption" color="text.secondary" sx={{ mr: 0.5 }}>Jump to</Typography>
            {graph.frames.map((f) => (
              <Chip key={f.id} size="small" variant="outlined" clickable label={f.title}
                    onClick={() => canvas.current?.frame(f.id)} />
            ))}
            <Chip size="small" color="primary" variant="outlined" clickable label="Everything"
                  onClick={() => canvas.current?.fit()} />
          </Stack>
          <Paper variant="outlined" sx={{ borderColor: '#000', overflow: 'hidden' }}>
            <NodeGraph ref={canvas} nodes={nodes} edges={graph.edges} frames={graph.frames}
                       size={{ w: graph.width, h: graph.height }} viewHeight="72vh"
                       label="The whole migration system, from tenant setup to the dashboards"
                       selected={selected} onSelect={setSelected} bright={bright} />
          </Paper>
          <Stack direction="row" spacing={2} sx={{ mt: 1.5, flexWrap: 'wrap', rowGap: 1, alignItems: 'center' }}>
            <NodeGraphLegend />
            <Typography variant="caption" color="text.secondary">
              drag to pan · ctrl/⌘ + scroll or the buttons to zoom · esc clears
            </Typography>
          </Stack>
        </Box>

        <Paper variant="outlined" data-testid="pipeline-detail"
               sx={{ width: { xs: '100%', lg: 340 }, flexShrink: 0, p: 2, maxHeight: '78vh', overflowY: 'auto' }}>
          {!sel ? (
            <>
              <Typography variant="subtitle1" sx={{ fontWeight: 700, mb: 1 }}>What you are looking at</Typography>
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
                {graph.nodes.length} parts in {graph.frames.length} sections, joined by {EDGES.length} wires.
                Select one and everything upstream and downstream of it stays lit.
              </Typography>
              <Stack direction="row" flexWrap="wrap" gap={0.75}>
                {(Object.keys(ROLE_COLOR) as Role[]).map((r) => (
                  <Chip key={r} size="small" label={ROLE_LABEL[r]}
                        sx={{ bgcolor: ROLE_COLOR[r], color: '#fff' }} />
                ))}
              </Stack>
            </>
          ) : (
            <Stack spacing={1.25} data-testid="pipeline-selected">
              <Box>
                <Typography variant="h6" sx={{ fontWeight: 700, lineHeight: 1.2 }}>{sel.title}</Typography>
                <Typography variant="caption" color="text.secondary"
                            sx={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }}>{sel.sub}</Typography>
              </Box>
              <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
                <Chip size="small" label={ROLE_LABEL[sel.role]} sx={{ bgcolor: ROLE_COLOR[sel.role], color: '#fff' }} />
                <Chip size="small" variant="outlined" label={`runs on: ${sel.where}`} />
              </Stack>
              {liveLine ? (
                <Stack direction="row" spacing={1} alignItems="center">
                  <Box sx={{ width: 10, height: 10, borderRadius: '50%', bgcolor: liveLine.color, flexShrink: 0 }} />
                  <Typography variant="body2" data-testid="pipeline-live"
                              sx={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }}>{liveLine.text}</Typography>
                </Stack>
              ) : (
                <Typography variant="caption" color="text.disabled">
                  {sel.live || sel.stage ? 'No live reading right now.' : 'Nothing measures this part live.'}
                </Typography>
              )}
              <Typography variant="body2">{sel.about}</Typography>

              {sel.files.length > 0 && (
                <Box>
                  <Typography variant="overline" color="text.secondary">Where it lives</Typography>
                  {sel.files.map((f) => (
                    <Typography key={f} variant="caption" component="div"
                                sx={{ fontFamily: 'ui-monospace, Menlo, Consolas, monospace' }}>{f}</Typography>
                  ))}
                </Box>
              )}
              {sel.knobs && sel.knobs.length > 0 && (
                <Box>
                  <Typography variant="overline" color="text.secondary">Tuned by</Typography>
                  <Box>{sel.knobs.map((k) => <Chip key={k} size="small" variant="outlined" label={k} sx={{ mr: 0.5, mb: 0.5 }} />)}</Box>
                </Box>
              )}
              <Box>
                <Typography variant="overline" color="text.secondary">
                  Comes from · {trace?.up.size ?? 0} upstream
                </Typography>
                <Box>{fedBy.length ? fedBy.map((e) => <NodeLink key={`${e.from}${e.label}`} id={e.from} label={e.label} onGo={go} />)
                  : <Typography variant="caption" color="text.disabled">Nothing — a starting point.</Typography>}</Box>
              </Box>
              <Box>
                <Typography variant="overline" color="text.secondary">
                  Feeds · {trace?.down.size ?? 0} downstream
                </Typography>
                <Box>{feeds.length ? feeds.map((e) => <NodeLink key={`${e.to}${e.label}`} id={e.to} label={e.label} onGo={go} />)
                  : <Typography variant="caption" color="text.disabled">Nothing — an end point.</Typography>}</Box>
              </Box>
              {sel.page && (
                <Button size="small" variant="outlined" endIcon={<OpenIcon fontSize="small" />}
                        onClick={() => navigate(sel.page!)}>Open the page for this</Button>
              )}
              <Button size="small" onClick={() => canvas.current?.fitNodes(bright ?? [])}>
                Zoom to its path
              </Button>
              <Button size="small" onClick={() => setSelected(null)}>Clear selection</Button>
            </Stack>
          )}
        </Paper>
      </Stack>
    </Box>
  )
}

export default Pipeline
