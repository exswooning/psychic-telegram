import React from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  CardHeader,
  Switch,
  FormControlLabel,
  Alert,
} from '@mui/material'
import { Settings as SettingsIcon, Security as SecurityIcon } from '@mui/icons-material'
import { useMigrationStore } from '@/store'

/**
 * Deploy (VPS connection + push-to-host + history) moved to its own page
 * -- real, wired capability, not "settings" in the usual sense. What's
 * left here is what's actually real: the dark-mode toggle (store-backed)
 * and an honest note about the access model. The "Migration Defaults"
 * card, the 5 clickable-looking color swatches, and the "Security"
 * toggles that used to live here were never wired to anything --
 * `defaultChecked` with no onChange, a Slider that saved nowhere, a
 * "Save Settings" button with no handler. Removed rather than kept as
 * decoration.
 */
const Settings: React.FC = () => {
  const { darkMode, toggleDarkMode } = useMigrationStore()

  return (
    <Box>
      <Typography variant="h4" sx={{ fontWeight: 700, mb: 0.5 }}>Settings</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>Appearance and access.</Typography>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardHeader title="Appearance" avatar={<SettingsIcon />} />
        <CardContent>
          <FormControlLabel
            control={<Switch checked={darkMode} onChange={toggleDarkMode} />}
            label="Dark Mode"
          />
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider', mb: 3 }}>
        <CardHeader title="Access" avatar={<SecurityIcon />} />
        <CardContent>
          <Alert severity="info">
            webui.py and api_server.py both bind 127.0.0.1 only -- access is
            controlled by the SSH tunnel, not by a setting on this page. See
            the Deploy page for reaching them from your own machine.
          </Alert>
        </CardContent>
      </Card>

      <Card elevation={0} sx={{ borderRadius: 2, border: '1px solid', borderColor: 'divider' }}>
        <CardHeader title="About" />
        <CardContent>
          <Typography variant="body2">Bitport</Typography>
          <Typography variant="caption" color="text.secondary">Built with React, Material UI, and TypeScript</Typography>
        </CardContent>
      </Card>
    </Box>
  )
}

export default Settings
