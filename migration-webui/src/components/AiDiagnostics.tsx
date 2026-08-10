import React, { useCallback, useEffect, useState } from 'react'
import {
  Alert, Box, Button, Chip, CircularProgress, Collapse, Divider, Link,
  Paper, Stack, TextField, Typography,
} from '@mui/material'
import {
  AutoAwesome as AiIcon, Visibility as PeekIcon,
} from '@mui/icons-material'
import {
  AiStatus, AiAnalysis, fetchAiStatus, saveAiKey, fetchAiContext,
  runAiAnalysis, getOperator,
} from '@/api/controlPlane'
import ReasonCodeDialog from './ReasonCodeDialog'

/**
 * "What is going on right now?", answered by a model reading the ledger,
 * the process table and the run log at once.
 *
 * Diagnosing this migration means correlating four sources that live in
 * four places, and every expensive mistake on this project has been a
 * *missed* signal rather than a misread one: 20,714 ACL grants 404ing under
 * a "0 file failures" summary, 93 files left world-readable, a benchmark
 * reporting PASS for a run that copied nothing. This is a second pair of
 * eyes on that specific failure mode.
 *
 * Two things it deliberately does not do.
 *
 * It never acts. The output is prose. Nothing here retries an item, gates a
 * run or writes to a tenant, because a model that can be wrong is fine as a
 * reader and unacceptable as a control.
 *
 * It never sends anything silently. The log tail carries real user
 * addresses and real file names, and "Show what will be sent" returns the
 * exact payload without transmitting it. An operator shipping tenant data
 * to a third party should be doing it knowingly.
 */

/** Minimal Markdown -> React. The model is asked for bold headers, bullets
 *  and fenced code, so that is what this handles; anything else renders as
 *  plain text rather than as broken markup. A full parser would be a
 *  dependency, and the artifact CSP blocks CDN scripts anyway. */
const Markdown: React.FC<{ text: string }> = ({ text }) => {
  const blocks: React.ReactNode[] = []
  const lines = text.split('\n')
  let code: string[] | null = null
  let list: string[] = []

  const flushList = (key: string) => {
    if (!list.length) return
    blocks.push(
      <Box key={key} component="ul" sx={{ pl: 3, my: 0.5 }}>
        {list.map((li, i) => (
          <Typography key={i} component="li" variant="body2" sx={{ mb: 0.25 }}>
            {inline(li)}
          </Typography>
        ))}
      </Box>,
    )
    list = []
  }

  lines.forEach((raw, i) => {
    const line = raw.replace(/\s+$/, '')
    if (line.trim().startsWith('```')) {
      if (code) {
        blocks.push(
          <Box key={`c${i}`} component="pre" sx={{
            fontSize: 11, p: 1.5, my: 1, bgcolor: 'action.hover', borderRadius: 1,
            overflowX: 'auto', whiteSpace: 'pre-wrap', wordBreak: 'break-word', m: 0,
          }}>{code.join('\n')}</Box>,
        )
        code = null
      } else {
        flushList(`l${i}`)
        code = []
      }
      return
    }
    if (code) { code.push(raw); return }

    if (/^\s*[-*]\s+/.test(line)) { list.push(line.replace(/^\s*[-*]\s+/, '')); return }
    flushList(`l${i}`)

    if (!line.trim()) return
    const heading = line.match(/^#{1,4}\s+(.*)$/)
    // The prompt asks for **Status** / **Problems** / **Progress**, so a
    // line that is entirely bold is a section header, not emphasis.
    const boldOnly = line.match(/^\*\*(.+?)\*\*:?\s*$/)
    if (heading || boldOnly) {
      blocks.push(
        <Typography key={`h${i}`} variant="subtitle2"
                    sx={{ fontWeight: 700, mt: 1.5, mb: 0.5 }}>
          {heading ? heading[1] : boldOnly![1]}
        </Typography>,
      )
      return
    }
    blocks.push(
      <Typography key={`p${i}`} variant="body2" sx={{ mb: 0.5 }}>
        {inline(line)}
      </Typography>,
    )
  })
  flushList('last')
  // Aliased because TypeScript cannot see that the forEach callback above
  // reassigns `code`, so it narrows it to `never` here.
  const unterminated = code as string[] | null
  if (unterminated) {
    // An unclosed fence means the model's reply was truncated mid-block;
    // showing the partial code is more useful than dropping it silently.
    blocks.push(
      <Box key="ctail" component="pre" sx={{
        fontSize: 11, p: 1.5, bgcolor: 'action.hover', borderRadius: 1,
        overflowX: 'auto', whiteSpace: 'pre-wrap', m: 0,
      }}>{unterminated.join('\n')}</Box>,
    )
  }
  return <>{blocks}</>
}

/** `**bold**` and `` `code` `` only. Split on both at once so a line
 *  containing each does not lose one of them. */
function inline(s: string): React.ReactNode {
  const parts = s.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)
  return parts.map((p, i) => {
    if (p.startsWith('**') && p.endsWith('**')) {
      return <strong key={i}>{p.slice(2, -2)}</strong>
    }
    if (p.startsWith('`') && p.endsWith('`') && p.length > 2) {
      return (
        <Box key={i} component="code" sx={{
          fontFamily: 'ui-monospace, monospace', fontSize: '0.85em',
          bgcolor: 'action.hover', px: 0.5, borderRadius: 0.5,
        }}>{p.slice(1, -1)}</Box>
      )
    }
    return p
  })
}

const AiDiagnostics: React.FC = () => {
  const [status, setStatus] = useState<AiStatus | null>(null)
  const [keyInput, setKeyInput] = useState('')
  const [prompt, setPrompt] = useState('')
  const [askReason, setAskReason] = useState(false)
  const [saving, setSaving] = useState(false)
  const [saveErr, setSaveErr] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<AiAnalysis | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [peek, setPeek] = useState<string | null>(null)

  const refresh = useCallback(() => {
    fetchAiStatus().then(setStatus).catch(() => {})
  }, [])
  useEffect(() => { refresh() }, [refresh])

  const save = async (reason: string) => {
    setSaving(true); setSaveErr(null)
    try {
      await saveAiKey(reason, keyInput.trim())
      setKeyInput(''); setAskReason(false); refresh()
    } catch (e: any) {
      setSaveErr(e.message)
    } finally {
      setSaving(false)
    }
  }

  const analyze = async () => {
    setBusy(true); setError(null); setResult(null)
    try {
      const r = await runAiAnalysis(prompt)
      if (r.error) setError(r.error)
      else setResult(r)
    } catch (e: any) {
      setError(e.message)
    } finally {
      setBusy(false)
    }
  }

  const showPayload = async () => {
    if (peek) { setPeek(null); return }
    try {
      const r = await fetchAiContext(prompt)
      setPeek(r.context)
    } catch (e: any) {
      setError(e.message)
    }
  }

  const isAdmin = !!getOperator()

  return (
    <Paper variant="outlined" sx={{ borderRadius: 2, p: 2 }}>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1, flexWrap: 'wrap', gap: 1 }}>
        <AiIcon color="action" />
        <Typography variant="h6" sx={{ flexGrow: 1 }}>What&apos;s going on?</Typography>
        {status?.configured && (
          <Chip size="small" variant="outlined" label={status.model} />
        )}
      </Stack>

      <Typography variant="caption" color="text.secondary">
        Reads the ledger, the running processes and the newest log, and
        summarises them. Advisory only — nothing here retries, gates or
        changes anything.
      </Typography>

      {!status?.configured && (
        <Box sx={{ mt: 2 }}>
          <Alert severity="info" sx={{ mb: 1.5 }}>
            Needs a Groq API key. It is stored in <code>env.sh</code> and
            shared with the classic UI&apos;s own analyze panel.{' '}
            <Link href="https://console.groq.com/keys" target="_blank" rel="noreferrer">
              console.groq.com/keys
            </Link>
          </Alert>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <TextField
              size="small" type="password" label="Groq API key"
              placeholder="gsk_..." value={keyInput}
              onChange={(e) => setKeyInput(e.target.value)}
              sx={{ width: { xs: '100%', sm: 320 } }}
            />
            <Button
              variant="contained"
              disabled={!isAdmin || keyInput.trim().length < 10}
              onClick={() => setAskReason(true)}
            >
              Save key
            </Button>
          </Stack>
          {!isAdmin && (
            <Typography variant="caption" color="text.secondary">
              Set an operator name to save a key.
            </Typography>
          )}
        </Box>
      )}

      {status?.configured && (
        <Box sx={{ mt: 2 }}>
          <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
            <TextField
              size="small" label="Ask something specific (optional)"
              placeholder="why did info@ fail?"
              value={prompt} onChange={(e) => setPrompt(e.target.value)}
              sx={{ width: { xs: '100%', sm: 360 } }}
            />
            <Button variant="contained" onClick={analyze} disabled={busy}
                    startIcon={busy ? <CircularProgress size={16} /> : <AiIcon />}>
              {busy ? 'Reading the logs…' : 'Analyze'}
            </Button>
            <Button size="small" startIcon={<PeekIcon />} onClick={showPayload}>
              {peek ? 'Hide' : 'Show what will be sent'}
            </Button>
          </Stack>

          {status.logFile && (
            <Typography variant="caption" color="text.secondary"
                        sx={{ display: 'block', mt: 1 }}>
              Newest log: <code>{status.logFile}</code> · key {status.keyMask}
            </Typography>
          )}

          <Collapse in={!!peek}>
            <Alert severity="warning" sx={{ mt: 2 }}>
              This is the exact payload. It contains real addresses and file
              names, and sending it transmits them to Groq.
            </Alert>
            <Box component="pre" sx={{
              mt: 1, fontSize: 11, p: 1.5, bgcolor: 'action.hover',
              borderRadius: 1, overflowX: 'auto', maxHeight: 300,
              whiteSpace: 'pre-wrap', wordBreak: 'break-word', m: 0,
            }}>{peek}</Box>
          </Collapse>

          {error && <Alert severity="error" sx={{ mt: 2 }}>{error}</Alert>}

          {result && (
            <Box sx={{ mt: 2 }}>
              <Divider sx={{ mb: 1.5 }} />
              <Markdown text={result.markdown} />
              <Typography variant="caption" color="text.secondary"
                          sx={{ display: 'block', mt: 2 }}>
                Generated by {result.model} from {result.context.length.toLocaleString()}{' '}
                characters of live state. Treat it as a lead, not a finding —
                verify anything you act on.
              </Typography>
            </Box>
          )}
        </Box>
      )}

      <ReasonCodeDialog
        open={askReason} busy={saving} error={saveErr}
        title="Save the Groq API key"
        description={
          <>
            Writes <code>GROQ_API_KEY</code> to <code>env.sh</code> on this
            host. It changes nothing in either tenant, but it is a credential,
            so the action is recorded against your name.
          </>
        }
        onCancel={() => { setAskReason(false); setSaveErr(null) }}
        onConfirm={save}
      />
    </Paper>
  )
}

export default AiDiagnostics
