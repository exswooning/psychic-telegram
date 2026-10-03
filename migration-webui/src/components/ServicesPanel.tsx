import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Card, CardContent, Chip, Dialog, DialogActions, DialogContent,
  DialogTitle, Stack, Typography,
} from '@mui/material'
import { RestartAlt as RestartIcon } from '@mui/icons-material'
import { fetchHostServices, restartHostUnit, HostServices, HostUnit } from '@/api/client'

const mono = { fontFamily: 'ui-monospace, monospace', fontSize: 12 }

/**
 * The box's own services -- `systemctl is-active`, `journalctl -p warning`, the
 * kernel's OOM kills and DEPLOYED_COMMIT, all of which took SSH before. Restart
 * is refused while any job runs, the same rule a deploy follows: a restart kills
 * a webui-launched seed or migration with it.
 */
const ServicesPanel: React.FC = () => {
  const [data, setData] = useState<HostServices | null>(null)
  const [asking, setAsking] = useState<HostUnit | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  const load = useCallback(() => {
    fetchHostServices().then(setData).catch(() => setData(null))
  }, [])
  useEffect(() => { load(); const id = setInterval(load, 15000); return () => clearInterval(id) }, [load])

  if (!data) return null
  if (!data.ok) return null          // not a superadmin: the host is not this account's
  const busy = data.busy ?? []

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}
          data-testid="services-panel">
      <CardContent>
        <Stack direction="row" alignItems="baseline" spacing={2} sx={{ mb: 1.5, flexWrap: 'wrap' }}>
          <Typography variant="h6" sx={{ fontWeight: 600 }}>Services</Typography>
          <Typography variant="body2" color="text.secondary" data-testid="deployed-commit">
            deployed commit <Box component="span" sx={mono}>{data.deployed_commit || 'unknown'}</Box>
          </Typography>
        </Stack>
        {msg && <Alert severity={msg.ok ? 'success' : 'warning'} sx={{ mb: 1.5 }}
                       onClose={() => setMsg(null)}>{msg.text}</Alert>}
        <Stack spacing={1.5}>
          {data.units.map((u) => (
            <Box key={u.unit} data-testid={`unit-${u.unit}`}>
              <Stack direction="row" spacing={1.5} alignItems="center" sx={{ flexWrap: 'wrap', gap: 1 }}>
                <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 130 }}>{u.unit}</Typography>
                <Chip size="small" label={`${u.active}${u.sub ? ` (${u.sub})` : ''}`}
                      color={u.active === 'active' ? 'success' : 'error'} />
                <Typography variant="caption" color="text.secondary">
                  since {u.since || '—'} · restarted {u.restarts}×
                </Typography>
                {u.unit !== 'caddy' && (
                  <Button size="small" startIcon={<RestartIcon />} disabled={busy.length > 0}
                          title={busy.length ? `Refused while running: ${busy.join('; ')}` : ''}
                          onClick={() => setAsking(u)} data-testid={`restart-${u.unit}`}>
                    Restart
                  </Button>
                )}
              </Stack>
              {u.recent.length > 0 && (
                <Box component="pre" sx={{ ...mono, m: 0, mt: 0.5, p: 1, bgcolor: 'action.hover',
                                           borderRadius: 1, whiteSpace: 'pre-wrap', maxHeight: 160,
                                           overflow: 'auto' }}>
                  {u.recent.join('\n')}
                </Box>
              )}
            </Box>
          ))}
        </Stack>
        {busy.length > 0 && (
          <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 1.5 }}>
            Restart is off while these run: {busy.join('; ')}
          </Typography>
        )}
        {data.oom.length > 0 && (
          <Alert severity="error" sx={{ mt: 1.5 }} data-testid="oom-kills">
            <Typography variant="body2" sx={{ fontWeight: 600 }}>
              The kernel killed a process for memory in the last 7 days:
            </Typography>
            <Box component="pre" sx={{ ...mono, m: 0, whiteSpace: 'pre-wrap' }}>{data.oom.join('\n')}</Box>
          </Alert>
        )}
      </CardContent>
      <Dialog open={!!asking} onClose={() => setAsking(null)}>
        <DialogTitle>Restart {asking?.unit}?</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            Nothing is running, so nothing is cut off. The page reconnects on its own
            once the service is back, usually within a few seconds.
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAsking(null)}>Cancel</Button>
          <Button color="warning" variant="contained" data-testid="restart-confirm"
                  onClick={async () => {
                    const u = asking!
                    setAsking(null)
                    const r = await restartHostUnit(u.unit)
                    setMsg({ ok: r.ok, text: r.msg })
                    setTimeout(load, 4000)
                  }}>
            Restart
          </Button>
        </DialogActions>
      </Dialog>
    </Card>
  )
}

export default ServicesPanel
