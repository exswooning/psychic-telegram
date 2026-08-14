import { createTheme, ThemeOptions } from '@mui/material/styles'

// Google's own tokens, not a generic Material palette -- the exact blue,
// grays, and type pairing Workspace Admin Console, Gmail, and Search
// actually use (#1a73e8, #f8f9fa, #5f6368, Google Sans + Roboto), so this
// reads as "Google" specifically rather than "Material Design" generally.
// Layout borrows the other half of the brief -- CloudM Migrate's own
// enterprise-migration-tool structure (sidebar, batch/status cards,
// per-item progress) -- from the existing page components; this file is
// the palette/type/shape system beneath them.
const baseOptions: ThemeOptions = {
  typography: {
    fontFamily: '"Roboto", "Google Sans Flex", -apple-system, "Segoe UI", sans-serif',
    h1: { fontFamily: '"Google Sans Flex", "Roboto", sans-serif', fontWeight: 500, fontSize: '2.375rem', lineHeight: 1.25, letterSpacing: '-0.25px' },
    h2: { fontFamily: '"Google Sans Flex", "Roboto", sans-serif', fontWeight: 500, fontSize: '2rem', lineHeight: 1.25, letterSpacing: '-0.25px' },
    h3: { fontFamily: '"Google Sans Flex", "Roboto", sans-serif', fontWeight: 500, fontSize: '1.75rem', lineHeight: 1.29 },
    h4: { fontFamily: '"Google Sans Flex", "Roboto", sans-serif', fontWeight: 500, fontSize: '1.5rem', lineHeight: 1.33 },
    h5: { fontFamily: '"Google Sans Flex", "Roboto", sans-serif', fontWeight: 500, fontSize: '1.375rem', lineHeight: 1.27 },
    h6: { fontFamily: '"Google Sans Flex", "Roboto", sans-serif', fontWeight: 500, fontSize: '1rem', letterSpacing: '0.1px', lineHeight: 1.5 },
    subtitle1: { fontWeight: 500, fontSize: '0.875rem', letterSpacing: '0.1px', lineHeight: 1.43 },
    subtitle2: { fontWeight: 500, fontSize: '0.75rem', letterSpacing: '0.2px', lineHeight: 1.33 },
    body1: { fontSize: '1rem', letterSpacing: '0.1px', lineHeight: 1.5 },
    body2: { fontSize: '0.875rem', letterSpacing: '0.1px', lineHeight: 1.43 },
    caption: { fontSize: '0.75rem', lineHeight: 1.33 },
    overline: { fontSize: '0.688rem', letterSpacing: '0.5px', lineHeight: 1.45, textTransform: 'uppercase' },
    button: { textTransform: 'none', fontWeight: 500, fontSize: '0.875rem', letterSpacing: '0.1px' },
  },
  shape: { borderRadius: 8 },
  spacing: 8,
  transitions: {
    duration: { shortest: 150, shorter: 200, short: 250, standard: 300, complex: 375, enteringScreen: 225, leavingScreen: 195 },
  },
  // Google's own surfaces read as flat -- separation comes from a 1px
  // border (#dadce0-family, set per-palette below), not elevation. A
  // single low, wide shadow stands in for MUI's 25-step scale so anything
  // that still asks for elevation (menus, popovers) gets one consistent,
  // barely-there shadow instead of the default's much heavier one.
  shadows: Array(25).fill('0 1px 3px 0 rgba(60,64,67,0.15), 0 1px 2px 0 rgba(60,64,67,0.10)') as any,
  components: {
    MuiCard: {
      styleOverrides: {
        root: { borderRadius: 8, boxShadow: 'none', border: '1px solid' },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { backgroundImage: 'none' },
        rounded: { borderRadius: 8 },
        outlined: { boxShadow: 'none' },
      },
    },
    MuiButton: {
      styleOverrides: {
        // Fully rounded is Material 3's actual spec default for filled/
        // outlined buttons (not a stylistic add-on) -- kept, not softened.
        root: { borderRadius: 999, padding: '9px 20px', fontWeight: 500 },
        contained: { boxShadow: 'none', '&:hover': { boxShadow: 'none' } },
        sizeSmall: { padding: '6px 14px' },
      },
    },
    MuiIconButton: {
      styleOverrides: { root: { borderRadius: 999 } },
    },
    MuiTextField: {
      styleOverrides: { root: { '& .MuiOutlinedInput-root': { borderRadius: 8 } } },
    },
    MuiChip: {
      styleOverrides: {
        root: { borderRadius: 999, fontWeight: 500, fontSize: '0.8125rem' },
      },
    },
    MuiTableCell: {
      styleOverrides: {
        root: { borderBottom: '1px solid' },
        head: { fontWeight: 500, fontSize: '0.8125rem' },
      },
    },
    MuiLinearProgress: { styleOverrides: { root: { borderRadius: 999, height: 6 } } },
    MuiCircularProgress: { styleOverrides: { root: { color: '#1a73e8' } } },
    MuiListItemButton: {
      styleOverrides: { root: { borderRadius: 999 } },
    },
    MuiAppBar: {
      styleOverrides: { root: { boxShadow: 'none' } },
    },
  },
}

// Workspace Admin Console's actual light palette -- #1a73e8 (not the
// generic Material #1976d2), #f8f9fa page ground, #5f6368 secondary text,
// #dadce0 borders. Every one of these is a real, load-bearing Google UI
// color, not an approximation.
const lightPalette = {
  mode: 'light' as const,
  primary: { main: '#1a73e8', light: '#d2e3fc', dark: '#185abc', contrastText: '#ffffff' },
  secondary: { main: '#188038', light: '#e6f4ea', dark: '#0d652d', contrastText: '#ffffff' },
  success: { main: '#188038', light: '#e6f4ea', dark: '#0d652d' },
  warning: { main: '#f9ab00', light: '#fef7e0', dark: '#805600' },
  error: { main: '#d93025', light: '#fce8e6', dark: '#a50e0e' },
  info: { main: '#1a73e8', light: '#d2e3fc', dark: '#185abc' },
  background: { default: '#f8f9fa', paper: '#ffffff' },
  text: { primary: '#202124', secondary: '#5f6368' },
  divider: '#dadce0',
}

// Google's own dark surfaces (Gmail/Search dark mode), not an inverted
// light palette -- #202124 ground, #8ab4f8 the specific lighter blue
// Google uses for primary-on-dark (a straight-inverted #1a73e8 loses too
// much contrast against a dark ground to read as a real Google surface).
const darkPalette = {
  mode: 'dark' as const,
  primary: { main: '#8ab4f8', light: '#aecbfa', dark: '#669df6', contrastText: '#202124' },
  secondary: { main: '#81c995', light: '#a8dab5', dark: '#5bb974', contrastText: '#202124' },
  success: { main: '#81c995', light: '#2a3b2e', dark: '#5bb974' },
  warning: { main: '#fdd663', light: '#3a2e10', dark: '#f9ab00' },
  error: { main: '#f28b82', light: '#3a1f1f', dark: '#ee675c' },
  info: { main: '#8ab4f8', light: '#22314f', dark: '#669df6' },
  background: { default: '#202124', paper: '#292a2d' },
  text: { primary: '#e8eaed', secondary: '#9aa0a6' },
  divider: '#3c4043',
}

export const theme = createTheme({
  ...baseOptions,
  palette: lightPalette,
})

export const darkTheme = createTheme({
  ...baseOptions,
  palette: darkPalette,
})
