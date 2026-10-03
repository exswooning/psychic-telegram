import React, { useCallback, useEffect, useState } from 'react'
import { Alert, Button, Paper, Stack, Typography } from '@mui/material'
import { fetchRateCeilings, forgetRateCeiling, RateCeiling } from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * The Drive rate each tenant side has proven it can take. A fresh run starts
 * there instead of the configured guess -- so one learned from the wrong signal
 * (live: a per-user 403, not a project limit) caps every run after it. Forgetting
 * one was a hand-written DELETE over SSH.
 */
const LearnedLimits: React.FC<{ accountId: number }> = ({ accountId }) => {
  const [rows, setRows] = useState<RateCeiling[] | null>(null)
  const [forgetting, setForgetting] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  const load = useCallback(() => {
    Promise.resolve().then(() => fetchRateCeilings(accountId))
      .then((r) => setRows(r?.ceilings ?? null)).catch(() => setRows(null))
  }, [accountId])
  useEffect(load, [load])

  if (!rows) return null
  return (
    <Paper variant="outlined" sx={{ p: 2, mb: 3 }} data-testid="learned-limits">
      <Typography variant="subtitle2" sx={{ fontWeight: 700 }}>Learned Drive rate limits</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        The rate each side has proven it can take. The next run starts here, never above the
        configured guess.
      </Typography>
      {rows.length === 0 && (
        <Typography variant="body2" color="text.secondary">
          None yet — every run starts from the configured guess and learns from Google's first refusal.
        </Typography>
      )}
      {rows.map((r) => (
        <Stack key={r.tenant} direction="row" spacing={2} alignItems="center" sx={{ py: 0.5 }}
               data-testid={`ceiling-${r.tenant}`}>
          <Typography variant="body2" sx={{ minWidth: 70, fontWeight: 600 }}>{r.tenant}</Typography>
          <Typography variant="body2">{r.ceiling.toFixed(1)} calls/s</Typography>
          <Typography variant="caption" color="text.secondary">learned {r.updated_at}</Typography>
          <Button size="small" color="warning" onClick={() => setForgetting(r.tenant)}
                  data-testid={`forget-${r.tenant}`}>Forget</Button>
        </Stack>
      ))}
      {done && <Alert severity="success" sx={{ mt: 1 }} onClose={() => setDone(null)}>{done}</Alert>}
      <ReasonCodeDialog
        open={!!forgetting} error={err}
        title={`Forget the learned ${forgetting ?? ''} limit`}
        description={<>The next run starts again from the configured guess and learns the real
          limit from Google's first refusal. A run already going keeps its own.</>}
        onCancel={() => { setForgetting(null); setErr(null) }}
        onConfirm={async (reason) => {
          try {
            const r = await forgetRateCeiling(accountId, forgetting!, reason)
            if (!r.ok) throw new Error(r.detail)
            setDone(r.detail); setForgetting(null); load()
          } catch (e) {
            setErr(e instanceof Error ? e.message : String(e))
          }
        }}
      />
    </Paper>
  )
}

export default LearnedLimits
