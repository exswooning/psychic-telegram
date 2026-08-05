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
} from '@mui/material'
import {
  Settings as SettingsIcon, Save as SaveIcon, Refresh as RefreshIcon,
  Security as SecurityIcon, Dns as DeployIcon,
} from '@mui/icons-material'
import { useMigrationStore } from '@/store'
import { fetchConfig, saveDeployConfig, runDeploy, DeployFields } from '@/api/client'

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
  const [deployMsg, setDeployMsg] = useState<string | null>(null)
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    fetchConfig().then((c) => {
      if (c.deploy) {
        setFields({ host: c.deploy.host, user: c.deploy.user,
                    port: c.deploy.port, key: c.deploy.key,
                    uiPort: c.deploy.ui_port })
      }
      setLoaded(true)
    })
  }, [])

  const save = async () => {
    setErr(null); setSaveMsg(null)
    const r = await saveDeployConfig(fields)
    if (r.ok) setSaveMsg('saved -- reused next time, in either UI')
    else setErr(r.error || 'could not save')
  }

  const deploy = async () => {
    setErr(null); setDeployMsg(null)
    let confirm: string | undefined
    if (includeCredentials) {
      confirm = window.prompt(
        `This copies service-account keys and OAuth tokens to ${fields.host}.\n\n` +
        'Those files can read every mailbox in both tenants.\n\n' +
        'Type DEPLOY to proceed:') || ''
      if (confirm !== 'DEPLOY') return
    }
    const r = await runDeploy(fields, includeCredentials, confirm)
    if (r.ok) setDeployMsg('deploy started -- watch the Activity Feed')
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
          <Button variant="contained" color="error" disabled={!fields.host} onClick={deploy}>
            Deploy now
          </Button>
        </Stack>
        {saveMsg && <Alert severity="success" sx={{ mt: 2 }}>{saveMsg}</Alert>}
        {deployMsg && <Alert severity="success" sx={{ mt: 2 }}>{deployMsg}</Alert>}
        {err && <Alert severity="error" sx={{ mt: 2 }}>{err}</Alert>}
      </CardContent>
    </Card>
  )
}

export default Settings