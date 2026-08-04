import { createTheme, ThemeOptions } from '@mui/material/styles'

const baseOptions: ThemeOptions = {
  typography: {
    fontFamily: '"Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    h1: { fontWeight: 700, fontSize: '2.5rem', lineHeight: 1.2 },
    h2: { fontWeight: 600, fontSize: '2rem', lineHeight: 1.3 },
    h3: { fontWeight: 600, fontSize: '1.5rem', lineHeight: 1.4 },
    h4: { fontWeight: 600, fontSize: '1.25rem', lineHeight: 1.4 },
    h5: { fontWeight: 500, fontSize: '1.125rem', lineHeight: 1.4 },
    h6: { fontWeight: 500, fontSize: '1rem', lineHeight: 1.4 },
    body1: { fontSize: '1rem', lineHeight: 1.6 },
    body2: { fontSize: '0.875rem', lineHeight: 1.6 },
    button: { textTransform: 'none', fontWeight: 500 },
  },
  shape: { borderRadius: 12 },
  spacing: 8,
  transitions: {
    duration: { shortest: 150, shorter: 200, short: 250, standard: 300, complex: 375, enteringScreen: 225, leavingScreen: 195 },
  },
  shadows: Array(25).fill('none') as any,
  components: {
    MuiCard: {
      styleOverrides: {
        root: {
          boxShadow: '0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.06)',
          border: '1px solid rgba(0,0,0,0.06)',
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: {
          boxShadow: '0 1px 3px rgba(0,0,0,0.08), 0 1px 2px rgba(0,0,0,0.06)',
        },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: { borderRadius: 8, padding: '8px 16px' },
        contained: { boxShadow: '0 1px 2px rgba(0,0,0,0.1)' },
      },
    },
    MuiTextField: {
      styleOverrides: { root: { '& .MuiOutlinedInput-root': { borderRadius: 8 } } },
    },
    MuiChip: { styleOverrides: { root: { borderRadius: 6 } } },
    MuiTableCell: { styleOverrides: { root: { borderBottom: '1px solid rgba(0,0,0,0.06)' } } },
    MuiLinearProgress: { styleOverrides: { root: { borderRadius: 4, height: 8 } } },
    MuiCircularProgress: { styleOverrides: { root: { color: '#1976d2' } } },
  },
}

const lightPalette = {
  mode: 'light' as const,
  primary: { main: '#1976d2', light: '#42a5f5', dark: '#1565c0', contrastText: '#fff' },
  secondary: { main: '#00695c', light: '#26a69a', dark: '#004d40', contrastText: '#fff' },
  success: { main: '#2e7d32', light: '#4caf50', dark: '#1b5e20' },
  warning: { main: '#ed6c02', light: '#ff9800', dark: '#e65100' },
  error: { main: '#d32f2f', light: '#ef5350', dark: '#c62828' },
  info: { main: '#0288d1', light: '#29b6f6', dark: '#01579b' },
  background: { default: '#f8fafc', paper: '#ffffff' },
  text: { primary: '#1e293b', secondary: '#64748b' },
  divider: 'rgba(0,0,0,0.08)',
}

const darkPalette = {
  mode: 'dark' as const,
  primary: { main: '#90caf9', light: '#bbdefb', dark: '#64b5f6', contrastText: '#1e293b' },
  secondary: { main: '#4db6ac', light: '#80cbc4', dark: '#26a69a', contrastText: '#1e293b' },
  success: { main: '#81c784', light: '#a5d6a7', dark: '#66bb6a' },
  warning: { main: '#ffb74d', light: '#ffcc80', dark: '#ffa726' },
  error: { main: '#e57373', light: '#ef9a9a', dark: '#ef5350' },
  info: { main: '#64b5f6', light: '#90caf9', dark: '#42a5f5' },
  background: { default: '#0f172a', paper: '#1e293b' },
  text: { primary: '#f1f5f9', secondary: '#94a3b8' },
  divider: 'rgba(255,255,255,0.12)',
}

export const theme = createTheme({
  ...baseOptions,
  palette: lightPalette,
})

export const darkTheme = createTheme({
  ...baseOptions,
  palette: darkPalette,
})