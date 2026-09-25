import React, { useEffect, useMemo, useState } from 'react'
import { Alert, Box, Paper, Typography } from '@mui/material'
import { fetchStages } from '@/api/client'
import { fetchFleet, FleetNode } from '@/api/controlPlane'
import type { MigrationStage } from '@/types'
import NodeGraph, { GEdge, GLive, GNode, GSock, Kind, NodeGraphLegend } from '@/components/NodeGraph'

/**
 * Pipeline -- what feeds what, where it runs, and how far along it is.
 *
 * The wiring is fixed (it is how the engine is built); the numbers on it are
 * not. Each stage node shows the ledger's own counter for that stage, taken
 * from the same /api/spa/stages the Mission Control page reads. Counts are
 * shown as counts -- never averaged into one percentage -- and a stage the
 * ledger does not track says so rather than showing a guess.
 */

const STATUS: Record<string, [string, string]> = {   // status -> [dot colour, word]
  completed: ['#3ddc84', 'done'], verified: ['#3ddc84', 'verified'],
  in_progress: ['#4aa8ff', 'running'], retrying: ['#4aa8ff', 'retrying'],
  failed: ['#ff5c5c', 'failed'], mismatch: ['#ff5c5c', 'mismatch'],
  needs_attention: ['#ffb02e', 'attention'], paused: ['#ffb02e', 'paused'],
  waiting: ['#777', 'waiting'], pending: ['#777', 'pending'], not_started: ['#777', 'not started'],
}
// Stages whose usersCompleted is a real per-user tally. Authentication,
// validation and the report are yes/no, so they get a status word only.
const COUNTED = new Set(['discovery', 'gmail', 'drive', 'calendar', 'contacts', 'chat', 'permissions'])

const HEAD = { tenant: '#83314a', keys: '#7a5f1c', ctl: '#3b5e3b', engine: '#246283',
               store: '#6a3f8f', out: '#3c3c8f' }
const s = (label: string, kind: Kind): GSock => ({ label, kind })

const ENGINES: [string, string, string, string][] = [   // id, title, file, stage
  ['drive', 'Drive', 'drive_engine.py', 'drive'],
  ['gmail', 'Gmail', 'gmail_engine.py', 'gmail'],
  ['calendar', 'Calendar', 'calendar_engine.py', 'calendar'],
  ['contacts', 'Contacts', 'contacts_engine.py', 'contacts'],
  ['chat', 'Chat', 'chat_engine.py', 'chat'],
  ['perms', 'Permissions', 'main.py syncacls', 'permissions'],
]

const W = 1600, H = 660

export const Pipeline: React.FC = () => {
  const [stages, setStages] = useState<MigrationStage[] | null>(null)
  const [fleet, setFleet] = useState<FleetNode[] | null>(null)
  const [err, setErr] = useState('')

  useEffect(() => {
    let alive = true
    const load = async () => {
      const [st, fl] = await Promise.allSettled([fetchStages(), fetchFleet()])
      if (!alive) return
      if (st.status === 'fulfilled') { setStages(st.value); setErr('') }
      else setErr(String(st.reason instanceof Error ? st.reason.message : st.reason))
      // Fleet is operator-only; a client without it just sees no runner line.
      setFleet(fl.status === 'fulfilled' ? fl.value : null)
    }
    load()
    const t = window.setInterval(load, 5_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  const graph = useMemo(() => {
    const live = (id: string): GLive | undefined => {
      const st = stages?.find((x) => x.id === id)
      if (!st) return undefined
      if (id === 'user_creation') return { color: '#777', text: 'not tracked' }
      const [color, word] = STATUS[st.status] ?? ['#777', st.status]
      const counted = COUNTED.has(id)
      return {
        color, active: st.status === 'in_progress',
        text: counted ? `${word} ${st.usersCompleted}/${st.usersTotal} users` : word,
      }
    }
    const running = fleet?.reduce((n, f) => n + f.users_running, 0) ?? 0
    const runner: GLive | undefined = fleet
      ? { color: fleet.length ? '#3ddc84' : '#777', active: running > 0,
          text: `${fleet.length} node(s) · ${running} running` }
      : undefined

    const nodes: GNode[] = [
      { id: 'src', title: 'Source tenant', sub: 'Workspace · read-only DWD', head: HEAD.tenant,
        x: 16, y: 150, outs: [s('Users', 'items'), s('Items', 'items')] },
      { id: 'keys', title: 'Service-account keys', sub: 'keys/<account>/*-sa.json', head: HEAD.keys,
        x: 16, y: 430, outs: [s('Source key', 'creds'), s('Target key', 'creds')] },
      { id: 'disc', title: 'Discovery', sub: 'main.py init-db --auto-map', head: HEAD.ctl,
        x: 248, y: 90, live: live('discovery'), ins: [s('Users', 'items')], outs: [s('Identity map', 'map')] },
      { id: 'dwd', title: 'Domain-wide delegation', sub: 'dwd_helper · verify_scopes', head: HEAD.keys,
        x: 248, y: 400, live: live('authentication'),
        ins: [s('Source key', 'creds'), s('Target key', 'creds')],
        outs: [s('Source token', 'creds'), s('Target token', 'creds')] },
      { id: 'run', title: 'Job runner', sub: 'webui Job · node_agent', head: HEAD.ctl,
        x: 480, y: 200, live: runner, ins: [s('Identity map', 'map')], outs: [s('Users', 'ctl')] },
      { id: 'prov', title: 'Provision accounts', sub: 'provision.py', head: HEAD.ctl,
        x: 480, y: 470, live: live('user_creation'),
        ins: [s('Target token', 'creds'), s('Identity map', 'map')], outs: [s('Accounts', 'items')] },
      { id: 'res', title: 'Pacing & retry', sub: 'resilience.py · rate/retry', head: HEAD.ctl,
        x: 712, y: 300,
        ins: [s('Source token', 'creds'), s('Target token', 'creds'), s('Work', 'ctl')],
        outs: [s('Paced work', 'ctl')] },
      ...ENGINES.map(([id, title, file, stage], i): GNode => ({
        id, title, sub: file, head: HEAD.engine, x: 944, y: 16 + i * 104, live: live(stage),
        ins: [s('Source data', 'items'), s('Paced work', 'ctl')],
        outs: [s('Writes', 'items'), s('Rows', 'map')],
      })),
      { id: 'ledger', title: 'Ledger', sub: 'migration.db · per account', head: HEAD.store,
        x: 1176, y: 170, ins: [s('Identity map', 'map'), s('Rows', 'map')], outs: [s('State', 'map')] },
      { id: 'target', title: 'Target tenant', sub: 'Workspace · read + write', head: HEAD.tenant,
        x: 1176, y: 440, ins: [s('Writes', 'items')], outs: [s('Data', 'items')] },
      { id: 'valid', title: 'Validation', sub: 'verification_payload', head: HEAD.out,
        x: 1408, y: 200, live: live('validation'),
        ins: [s('Ledger', 'map'), s('Source data', 'items'), s('Target data', 'items')] },
      { id: 'report', title: 'Final report', sub: 'report_payload', head: HEAD.out,
        x: 1408, y: 420, live: live('report'), ins: [s('Ledger', 'map')] },
    ]
    const e = (from: string, to: string): GEdge => ({ from, to })
    const edges: GEdge[] = [
      e('src.Users', 'disc.Users'),
      e('keys.Source key', 'dwd.Source key'), e('keys.Target key', 'dwd.Target key'),
      e('dwd.Source token', 'res.Source token'), e('dwd.Target token', 'res.Target token'),
      e('dwd.Target token', 'prov.Target token'),
      e('disc.Identity map', 'run.Identity map'), e('disc.Identity map', 'prov.Identity map'),
      e('disc.Identity map', 'ledger.Identity map'),
      e('run.Users', 'res.Work'),
      e('prov.Accounts', 'target.Writes'),
      ...ENGINES.flatMap(([id]) => [
        e('src.Items', `${id}.Source data`), e('res.Paced work', `${id}.Paced work`),
        e(`${id}.Writes`, 'target.Writes'), e(`${id}.Rows`, 'ledger.Rows'),
      ]),
      e('ledger.State', 'valid.Ledger'), e('src.Items', 'valid.Source data'),
      e('target.Data', 'valid.Target data'),
      e('ledger.State', 'report.Ledger'),
    ]
    return { nodes, edges }
  }, [stages, fleet])

  return (
    <Box sx={{ p: 3 }}>
      <Typography variant="h5" sx={{ fontWeight: 700 }}>Pipeline</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 760 }}>
        What feeds what, and which file does it. Hover a node to trace its
        wires; a wire pulses while the stage behind it is running. The
        wiring is how the engine is built; the counts on each node are read
        live from the ledger.
      </Typography>
      {err && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          Live counts unavailable ({err}) — showing the wiring only.
        </Alert>
      )}
      <Paper variant="outlined" sx={{ overflowX: 'auto', bgcolor: '#1b1b1b', borderColor: '#000' }}>
        <NodeGraph nodes={graph.nodes} edges={graph.edges} width={W} height={H}
                   label="Migration pipeline: source tenant through delegation, pacing and six engines into the target tenant and the ledger" />
      </Paper>
      <Box sx={{ mt: 1.5 }}><NodeGraphLegend /></Box>
    </Box>
  )
}

export default Pipeline
