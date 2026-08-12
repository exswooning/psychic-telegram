import React, { useEffect, useState } from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  CardHeader,
  Switch,
  FormControlLabel,
  Checkbox,
  TextField,
  Button,
  Stack,
  Alert,
  Divider,
  Slider,
  InputLabel,
  FormControl,
  Select,
  MenuItem,
  Grid,
  Table,
  TableBody,
  TableCell,
  TableContainer,
  TableHead,
  TableRow,
  Chip,
} from '@mui/material'
import {
  Settings as SettingsIcon, Save as SaveIcon, Refresh as RefreshIcon,
  Security as SecurityIcon, Dns as DeployIcon, Cable as ConnectIcon,
  CheckCircle as OkIcon, Error as ErrIcon, ContentCopy as CopyIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import {
  fetchConfig, saveDeployConfig, runDeploy, DeployFields,
  fetchDeployHistory, DeployHistoryEntry,
} from '@/api/client'
import { getCpBase, setCpBase, checkConnection } from '@/api/controlPlane'
import JobProgress from '@/components/JobProgress'

const Settings: React.FC = () => {
  const { darkMode, toggleDarkMode } = useMigrationStore()

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Settings</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Configure your migration tool preferences</Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardHeader title="Appearance" subheader="Customize the look and feel" avatar={<SettingsIcon />} />
        <CardContent>
          <FormControlLabel
            control={<Switch checked={darkMode} onChange={toggleDarkMode} />}
            label="Dark Mode"
          />
          <Box sx={{ mt: 3 }}>
            <Typography variant="body2" gutterBottom>Theme Color</Typography>
            <Stack direction="row" spacing={1}>
              {['primary', 'secondary', 'success', 'warning', 'error'].map((color) => (
                <Box
                  key={color}
                  sx={{
                    width: 32,
                    height: 32,
                    borderRadius: '50%',
                    bgcolor: `${color}.main`,
                    cursor: 'pointer',
                    border: '2px solid transparent',
                    '&:hover': { borderColor: 'text.primary' },
                  }}
                />
              ))}
            </Stack>
          </Box>
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardHeader title="Migration Defaults" subheader="Default settings for new migrations" avatar={<RefreshIcon />} />
        <CardContent>
          <Grid container spacing={3}>
            <Grid item xs={12} sm={6}>
              <FormControl fullWidth>
                <InputLabel>Default Services</InputLabel>
                <Select value="drive,gmail,calendar" label="Default Services">
                  <MenuItem value="drive,gmail,calendar">Drive, Gmail, Calendar</MenuItem>
                  <MenuItem value="drive,gmail">Drive, Gmail</MenuItem>
                  <MenuItem value="drive">Drive Only</MenuItem>
                  <MenuItem value="gmail">Gmail Only</MenuItem>
                </Select>
              </FormControl>
            </Grid>
            <Grid item xs={12} sm={6}>
              <TextField fullWidth label="Default Lookback (days)" type="number" defaultValue="30" variant="outlined" />
            </Grid>
            <Grid item xs={12}>
              <FormControlLabel control={<Switch defaultChecked />} label="Auto-resume on failure" />
            </Grid>
            <Grid item xs={12}>
              <Typography variant="body2" gutterBottom>Max Retries</Typography>
              <Slider defaultValue={3} min={0} max={10} step={1} marks={[{ value: 0, label: '0' }, { value: 5, label: '5' }, { value: 10, label: '10' }]} />
            </Grid>
          </Grid>
          <Button variant="contained" startIcon={<SaveIcon />} sx={{ mt: 2 }}>Save Settings</Button>
        </CardContent>
      </Card>

      <VpsConnectionCard />

      <DeployCard />

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardHeader title="Security" subheader="Authentication and access control" avatar={<SecurityIcon />} />
        <CardContent>
          <Alert severity="info" sx={{ mb: 2 }}>
            The web UI binds to localhost only. Access it via SSH tunnel for remote management.
          </Alert>
          <FormControlLabel control={<Switch defaultChecked />} label="Require confirmation for destructive actions" />
          <FormControlLabel control={<Switch defaultChecked />} label="Session timeout after 30 minutes" />
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardHeader title="About" subheader="Application information" />
        <CardContent>
          <Typography variant="body2">Google Workspace Migration Tool v1.0.0</Typography>
          <Typography variant="caption" color="text.secondary">Built with React, Material UI, and TypeScript</Typography>
        </CardContent>
      </Card>
    </Box>
  )
}

/**
 * Reaching the control plane (api_server.py, port 8090) is different from
 * reaching webui.py itself: this page is already loaded, so webui.py is
 * reachable by definition, but 8090 is a second port that only answers over
 * its own tunnel leg, and a browser cannot open an SSH tunnel on its own --
 * that has to happen in a terminal first (./connect_vps.sh). This card is
 * the missing feedback loop for that: it tells you whether the tunnel's
 * second leg actually came up, without a trip to devtools' Network tab.
 */
const VpsConnectionCard: React.FC = () => {
  const [base, setBase] = useState(getCpBase())
  const [checking, setChecking] = useState(false)
  const [result, setResult] = useState<Awaited<ReturnType<typeof checkConnection>> | null>(null)
  const [copied, setCopied] = useState(false)

  const test = async (urlToTest: string) => {
    setChecking(true); setResult(null)
    const r = await checkConnection(urlToTest)
    setResult(r)
    setChecking(false)
  }

  const save = () => {
    setCpBase(base)
    setResult(null)
  }

  const command = './connect_vps.sh'

  const copyCommand = () => {
    navigator.clipboard.writeText(command).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    })
  }

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
      <CardHeader title="VPS Connection" subheader="Reach the control plane through the SSH tunnel" avatar={<ConnectIcon />} />
      <CardContent>
        <Alert severity="info" sx={{ mb: 2 }}>
          Both webui.py and api_server.py bind 127.0.0.1 on the VPS -- the
          tunnel is the access control. Run this once per terminal session,
          from the repo on your own machine (not the VPS):
        </Alert>
        <Box sx={{
          display: 'flex', alignItems: 'center', gap: 1, mb: 2, p: 1.5,
          bgcolor: 'action.hover', borderRadius: 1,
          fontFamily: 'ui-monospace, monospace', fontSize: 13,
        }}>
          <Box component="code" sx={{ flexGrow: 1, wordBreak: 'break-all' }}>{command}</Box>
          <Button size="small" startIcon={<CopyIcon />} onClick={copyCommand}>
            {copied ? 'Copied' : 'Copy'}
          </Button>
        </Box>

        <Stack direction="row" spacing={2} sx={{ flexWrap: 'wrap', gap: 2 }}>
          <TextField
            size="small" label="Control plane address" value={base}
            onChange={(e) => setBase(e.target.value)}
            sx={{ width: { xs: '100%', sm: 280 } }}
            placeholder="http://localhost:8090"
          />
          <Button variant="outlined" disabled={checking} onClick={() => test(base)}>
            {checking ? 'Testing…' : 'Test connection'}
          </Button>
          <Button variant="contained" disabled={base === getCpBase()} onClick={save}>
            Use this address
          </Button>
        </Stack>

        {result && (
          <Alert
            sx={{ mt: 2 }}
            severity={result.ok ? 'success' : 'error'}
            icon={result.ok ? <OkIcon /> : <ErrIcon />}
          >
            {result.ok
              ? `Connected in ${result.ms}ms -- signed in as "${result.role}". `
                + (result.role === 'viewer'
                    ? 'No account session or recognized operator on this connection, so writes are refused.'
                    : '')
              : `Could not reach ${base}: ${result.error}. Is connect_vps.sh running, and does it forward this exact port?`}
          </Alert>
        )}

        <Typography variant="caption" color="text.secondary" sx={{ display: 'block', mt: 2 }}>
          Saved address is used by Mission Control's dashboard and every
          write action (provisioning, benchmarks, emergency revert) on this
          browser only -- it does not change what the tunnel forwards.
        </Typography>
      </CardContent>
    </Card>
  )
}

/**
 * The VPS connection webui.py's own Deploy relies on (deploy_remote.py --
 * copies this whole tool to a host that stays up through a multi-hour
 * migration, then you reach it over an SSH tunnel; see webui.py's module
 * docstring for why it copies the code rather than driving it remotely
 * over SSH). Saved to env.sh's DEPLOY_* entries, shared with webui.py's own
 * inline Deploy tab -- whichever UI saves last is what both read next.
 */
const DeployCard: React.FC = () => {
  const [fields, setFields] = useState<DeployFields>({
    host: '', user: 'root', port: '22', key: '',
  })
  const [includeCredentials, setIncludeCredentials] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [jobActive, setJobActive] = useState(false)
  const [jobRunning, setJobRunning] = useState(false)
  const [history, setHistory] = useState<DeployHistoryEntry[]>([])
  const wasRunning = React.useRef(false)

  useEffect(() => {
    fetchConfig().then((c) => {
      if (c.deploy) {
        setFields({ host: c.deploy.host, user: c.deploy.user,
                    port: c.deploy.port, key: c.deploy.key,
                    uiPort: c.deploy.ui_port })
      }
      setLoaded(true)
    })
    fetchDeployHistory().then(setHistory)
  }, [])

  // A running->stopped edge means the detached deploy just called
  // Job.start()'s on_finish callback server-side and wrote its rc --
  // refetch so the "in progress" row picks up the real result.
  useEffect(() => {
    if (wasRunning.current && !jobRunning) fetchDeployHistory().then(setHistory)
    wasRunning.current = jobRunning
  }, [jobRunning])

  const save = async () => {
    setErr(null); setSaveMsg(null)
    const r = await saveDeployConfig(fields)
    if (r.ok) setSaveMsg('saved -- reused next time, in either UI')
    else setErr(r.error || 'could not save')
  }

  const deploy = async () => {
    setErr(null)
    setJobActive(false)
    let confirm: string | undefined
    if (includeCredentials) {
      confirm = window.prompt(
        `This copies service-account keys and OAuth tokens to ${fields.host}.\n\n` +
        'Those files can read every mailbox in both tenants.\n\n' +
        'Type DEPLOY to proceed:') || ''
      if (confirm !== 'DEPLOY') return
    }
    const r = await runDeploy(fields, includeCredentials, confirm)
    if (r.ok) setJobActive(true)
    else setErr(r.error || 'could not start')
  }

  if (!loaded) return null

  return (
    <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
      <CardHeader title="Deploy to a VPS" subheader="Run the migration from a host that stays up" avatar={<DeployIcon />} />
      <CardContent>
        <Alert severity="info" sx={{ mb: 2 }}>
          Save your VPS's connection details once -- every future Deploy, from
          either this page or webui.py's own Deploy tab, reuses them.
        </Alert>
        <Grid container spacing={2}>
          <Grid item xs={12} sm={6}>
            <TextField
              fullWidth size="small" label="Host"
              placeholder="203.0.113.10 or vps.example.com"
              value={fields.host}
              onChange={(e) => setFields({ ...fields, host: e.target.value })}
            />
          </Grid>
          <Grid item xs={12} sm={3}>
            <TextField
              fullWidth size="small" label="SSH user"
              value={fields.user}
              onChange={(e) => setFields({ ...fields, user: e.target.value })}
            />
          </Grid>
          <Grid item xs={12} sm={3}>
            <TextField
              fullWidth size="small" label="SSH port"
              value={fields.port}
              onChange={(e) => setFields({ ...fields, port: e.target.value })}
            />
          </Grid>
          <Grid item xs={12}>
            <TextField
              fullWidth size="small" label="SSH key path (optional)"
              placeholder="~/.ssh/id_ed25519"
              value={fields.key}
              onChange={(e) => setFields({ ...fields, key: e.target.value })}
            />
          </Grid>
        </Grid>
        <FormControlLabel
          sx={{ mt: 1 }}
          control={<Checkbox checked={includeCredentials}
                            onChange={(e) => setIncludeCredentials(e.target.checked)} />}
          label={
            <Typography variant="body2">
              Also copy service-account keys and OAuth tokens -- lets this host read every mailbox in both tenants
            </Typography>
          }
        />
        <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
          <Button variant="outlined" startIcon={<SaveIcon />} onClick={save}>
            Save VPS credentials
          </Button>
          <Button variant="contained" color="error" disabled={!fields.host || jobRunning} onClick={deploy}>
            Deploy now
          </Button>
        </Stack>
        {saveMsg && <Alert severity="success" sx={{ mt: 2 }}>{saveMsg}</Alert>}
        {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
        <JobProgress active={jobActive} expectedName="deploy" onRunningChange={setJobRunning} />
        <DeployHistoryTable history={history} />
      </CardContent>
    </Card>
  )
}

/**
 * Every past /api/deploy call, most recent first -- previously the only
 * answer to "did the last deploy actually work, and what commit is running
 * on that VPS right now" was SSHing in and checking by hand.
 */
const DeployHistoryTable: React.FC<{ history: DeployHistoryEntry[] }> = ({ history }) => {
  if (!history.length) {
    return (
      <Typography variant="body2" color="text.secondary" sx={{ mt: 3 }}>
        No deploys recorded yet in this checkout.
      </Typography>
    )
  }
  return (
    <Box sx={{ mt: 3 }}>
      <Typography variant="subtitle2" fontWeight={600} sx={{ mb: 1 }}>Deploy history</Typography>
      <TableContainer sx={{ border: '1px solid', borderColor: 'divider', borderRadius: 1 }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Started</TableCell>
              <TableCell>Host</TableCell>
              <TableCell>Commit</TableCell>
              <TableCell>Creds</TableCell>
              <TableCell>Result</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {history.map((h) => (
              <TableRow key={h.id}>
                <TableCell>{new Date(h.startedAt).toLocaleString()}</TableCell>
                <TableCell>{h.host}</TableCell>
                <TableCell sx={{ fontFamily: 'ui-monospace, monospace' }}>
                  {h.commit || '(no git history)'}
                </TableCell>
                <TableCell>{h.includeCredentials ? 'yes' : 'no'}</TableCell>
                <TableCell>
                  {h.rc === null
                    ? <Chip size="small" label={h.finishedAt ? 'never started' : 'in progress'}
                           color={h.finishedAt ? 'error' : 'info'} variant="outlined" />
                    : <Chip size="small" label={h.rc === 0 ? 'ok' : `exit ${h.rc}`}
                           color={h.rc === 0 ? 'success' : 'error'} variant="outlined" />}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    </Box>
  )
}

export default Settings