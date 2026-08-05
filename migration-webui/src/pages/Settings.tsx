import React from 'react'
import {
  Box,
  Typography,
  Card,
  CardContent,
  CardHeader,
  Switch,
  FormControlLabel,
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
import { Settings as SettingsIcon, Save as SaveIcon, Refresh as RefreshIcon, Security as SecurityIcon } from '@mui/icons-material'
import { useMigrationStore } from '@/store'

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

export default Settings