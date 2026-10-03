import React, { useCallback, useEffect, useRef, useState } from 'react'
import {
  Alert, Box, Button, Card, CardContent, Checkbox, FormControlLabel, IconButton, MenuItem, Stack, TextField, Tooltip, Typography,
} from '@mui/material'
import { Refresh as RefreshIcon, Search as SearchIcon, Terminal as LogsIcon } from '@mui/icons-material'
import { fetchLogs } from '@/api/client'
import type { LogsPayload } from '@/api/client'
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
  /* Which transcript to read. A launched run's own stdout and stderr go to
     logs/jobs/{account}/{job}.log, so a traceback that kills a migration is
     in one of those files and in no other -- until this picker existed the
     only way to read one was to log in to the box. */
  const [jobs, setJobs] = useState<NonNullable<LogsPayload['jobs']>>([])
  const [selected, setSelected] = useState('')      // '' = the engine log
  const preRef = useRef<HTMLPreElement | null>(null)
  /* Search the WHOLE file -- the `grep -n -C2` this used to take over SSH. Run on
     demand, not on the poll: a migrate log runs to hundreds of MB. */
  const [query, setQuery] = useState('')
  const [search, setSearch] = useState<LogsPayload['search'] | null>(null)
  const runSearch = () => {
    const [job, account] = selected ? selected.split('|') : ['', '']
    if (!query.trim()) { setSearch(null); return }
    fetchLogs(job, account, query.trim())
      .then((r) => setSearch(r.search ?? null))
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }

  const refresh = useCallback(() => {
    const [job, account] = selected ? selected.split('|') : ['', '']
    fetchLogs(job, account)
      .then((r) => {
        setLines(r.lines); setPath(r.path)
        if (r.jobs) setJobs(r.jobs)
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [selected])

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
        {jobs.length > 0 && (
          <TextField select size="small" value={selected}
                     data-testid="log-picker"
                     onChange={(e) => { setSelected(e.target.value); setSearch(null) }}
                     sx={{ minWidth: 240, mr: 2 }}>
            <MenuItem value="">migration engine log</MenuItem>
            {jobs.map((j) => (
              <MenuItem key={`${j.account}/${j.job}`}
                        value={`${j.job}|${j.account}`}>
                {j.job} · account {j.account} · {Math.round(j.bytes / 1024)} KB
              </MenuItem>
            ))}
          </TextField>
        )}
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
          <Stack direction="row" spacing={1} sx={{ flexWrap: 'wrap', gap: 1 }}>
            <TextField size="small" label="Search the whole log" value={query}
                       placeholder="PASS 1/3, a file id, an error…" sx={{ minWidth: 280, flexGrow: 1 }}
                       inputProps={{ 'data-testid': 'log-search' }}
                       onChange={(e) => setQuery(e.target.value)}
                       onKeyDown={(e) => { if (e.key === 'Enter') runSearch() }} />
            <Button variant="outlined" startIcon={<SearchIcon />} onClick={runSearch}
                    data-testid="log-search-run">Search</Button>
          </Stack>
          {search && (
            <Box sx={{ mt: 1.5 }} data-testid="log-search-results">
              <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
                {search.total.toLocaleString()} matching line{search.total === 1 ? '' : 's'}
                {search.total > search.matches.length
                  ? ` — showing the last ${search.matches.length.toLocaleString()}` : ''}
              </Typography>
              <Box component="pre" sx={{
                m: 0, p: 1.5, bgcolor: 'background.default', borderRadius: 1,
                border: '1px solid', borderColor: 'divider', maxHeight: 500, overflow: 'auto',
                fontSize: 12, fontFamily: 'ui-monospace, monospace', whiteSpace: 'pre-wrap',
                wordBreak: 'break-word',
              }}>
                {search.matches.map((m) => (
                  <Box key={m.line} sx={{ mb: 1, pb: 1, borderBottom: '1px dashed', borderColor: 'divider' }}>
                    {m.before.map((l, i) => (
                      <Box key={`b${i}`} component="span" sx={{ display: 'block', color: 'text.secondary' }}>{l}</Box>))}
                    <Box component="span" sx={{ display: 'block', fontWeight: 700 }}>
                      {m.line}: {m.text}
                    </Box>
                    {m.after.map((l, i) => (
                      <Box key={`a${i}`} component="span" sx={{ display: 'block', color: 'text.secondary' }}>{l}</Box>))}
                  </Box>
                ))}
              </Box>
            </Box>
          )}
        </CardContent>
      </Card>

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
