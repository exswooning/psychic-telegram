import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Box, Typography, Card, CardContent, Stack, IconButton, Tooltip,
  FormControlLabel, Checkbox, Alert,
} from '@mui/material'
import { Refresh as RefreshIcon, Terminal as LogsIcon } from '@mui/icons-material'
import { fetchLogs } from '@/api/client'
import AiDiagnostics from '@/components/AiDiagnostics'

const ERROR_RE = /error/i
const WARN_RE = /warn/i

/**
 * Raw log tail (main.py's own log file) plus the existing AI diagnosis
 * panel -- real data (logs_payload(), already wired server-side), just
 * had zero frontend surface before this. AiDiagnostics already covers
 * "ask an LLM what's going on"; this covers "let me just read it myself".
 */
const Logs: React.FC = () => {
  const [lines, setLines] = useState<string[]>([]);
  const [path, setPath] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [follow, setFollow] = useState(true)
  const preRef = useRef<HTMLPreElement | null>(null)

  const refresh = useCallback(() => {
    fetchLogs()
      .then((r) => { setLines(r.lines); setPath(r.path) })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  useEffect(() => {
    refresh()
    const id = setInterval(refresh, 3000)
    return () => clearInterval(id)
  }, [refresh])

  useEffect(() => {
    if (follow && preRef.current) preRef.current.scrollTop = preRef.current.scrollHeight
  }, [lines, follow])

  return (
    <Box>
      <Stack direction="row" alignItems="center" sx={{ mb: 0.5 }}>
        <LogsIcon color="action" sx={{ mr: 1 }} />
        <Typography variant="h4" sx={{ fontWeight: 700, flexGrow: 1 }}>Logs</Typography>
        <Tooltip title="Re-check">
          <span>
            <IconButton size="small" onClick={refresh}>
              <RefreshIcon fontSize="small" />
            </IconButton>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        {path || 'The engine\'s own log file, tailed.'}
      </Typography>

      {error && <Alert severity="warning" sx={{ mb: 3 }}>{error}</Alert>}

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardContent>
          <FormControlLabel
            control={<Checkbox checked={follow} onChange={(e) => setFollow(e.target.checked)} size="small" />}
            label={<Typography variant="body2">Follow</Typography>}
            sx={{ mb: 1 }}
          />
          <Box
            component="pre" ref={preRef}
            sx={{
              m: 0, p: 1.5, bgcolor: 'background.default', borderRadius: 1,
              border: '1px solid', borderColor: 'divider', maxHeight: 500,
              overflow: 'auto', fontSize: 12, fontFamily: 'ui-monospace, monospace',
              whiteSpace: 'pre-wrap', wordBreak: 'break-word',
            }}
          >
            {lines.map((line, i) => (
              <Box key={i} component="span" sx={{
                display: 'block',
                color: ERROR_RE.test(line) ? 'error.main' : WARN_RE.test(line) ? 'warning.main' : 'inherit',
              }}>
                {line}
              </Box>
            ))}
            {lines.length === 0 && '(no log lines yet)'}
          </Box>
        </CardContent>
      </Card>

      <AiDiagnostics />
    </Box>
  )
}

export default Logs
