import { createTheme, ThemeOptions } from '@mui/material/styles'

// Tokens taken directly from DESIGN.md's Material-3-derived palette --
// "Systemic Operations": restrained functional color, pill-shaped controls,
// Plus Jakarta Sans headlines over Inter body/label text.
const baseOptions: ThemeOptions = {
  typography: {
    fontFamily: '"Inter", "Roboto", "Helvetica", "Arial", sans-serif',
    h1: { fontFamily: '"Plus Jakarta Sans", sans-serif', fontWeight: 400, fontSize: '2.375rem', lineHeight: 1.25 },   // display-lg-ish, capped
    h2: { fontFamily: '"Plus Jakarta Sans", sans-serif', fontWeight: 400, fontSize: '2rem', lineHeight: 1.25 },       // headline-lg
    h3: { fontFamily: '"Plus Jakarta Sans", sans-serif', fontWeight: 400, fontSize: '1.75rem', lineHeight: 1.29 },    // headline-md
    h4: { fontFamily: '"Plus Jakarta Sans", sans-serif', fontWeight: 400, fontSize: '1.5rem', lineHeight: 1.33 },     // headline-sm
    h5: { fontFamily: 'Inter, sans-serif', fontWeight: 500, fontSize: '1.375rem', lineHeight: 1.27 },                 // title-lg
    h6: { fontFamily: 'Inter, sans-serif', fontWeight: 500, fontSize: '1rem', letterSpacing: '0.15px', lineHeight: 1.5 }, // title-md
    subtitle1: { fontWeight: 500, fontSize: '0.875rem', letterSpacing: '0.1px', lineHeight: 1.43 },   // title-sm
    subtitle2: { fontWeight: 500, fontSize: '0.75rem', letterSpacing: '0.5px', lineHeight: 1.33 },    // label-md
    body1: { fontSize: '1rem', letterSpacing: '0.5px', lineHeight: 1.5 },       // body-lg
    body2: { fontSize: '0.875rem', letterSpacing: '0.25px', lineHeight: 1.43 }, // body-md
    caption: { fontSize: '0.75rem', lineHeight: 1.33 },                        // body-sm
    overline: { fontSize: '0.688rem', letterSpacing: '0.5px', lineHeight: 1.45, textTransform: 'uppercase' }, // label-sm
    button: { textTransform: 'none', fontWeight: 500, fontSize: '0.875rem', letterSpacing: '0.1px' }, // label-lg
  },
  shape: { borderRadius: 8 },
  spacing: 8,
  transitions: {
    duration: { shortest: 150, shorter: 200, short: 250, standard: 300, complex: 375, enteringScreen: 225, leavingScreen: 195 },
  },
  shadows: Array(25).fill('none') as any,
  components: {
    MuiCard: {
      styleOverrides: {
        root: {
          borderRadius: 12, // xl (0.75rem)
          boxShadow: '0 1px 2px rgba(60,64,67,0.3)',
          border: '1px solid',
        },
      },
    },
    MuiPaper: {
      styleOverrides: {
        root: { boxShadow: '0 1px 2px rgba(60,64,67,0.3)' },
        rounded: { borderRadius: 12 },
      },
    },
    MuiButton: {
      styleOverrides: {
        root: { borderRadius: 999, padding: '10px 20px' }, // pill-shaped, per DESIGN.md
        contained: { boxShadow: '0 1px 3px 1px rgba(60,64,67,0.15)' },
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
        root: { borderRadius: 999, fontWeight: 600 }, // status pills are fully rounded
      },
    },
    MuiTableCell: { styleOverrides: { root: { borderBottom: '1px solid' } } },
    MuiLinearProgress: { styleOverrides: { root: { borderRadius: 999, height: 8 } } },
    MuiCircularProgress: { styleOverrides: { root: { color: '#005bbf' } } },
    MuiListItemButton: {
      styleOverrides: { root: { borderRadius: 999 } }, // active nav pill
    },
  },
}

const lightPalette = {
  mode: 'light' as const,
  primary: { main: '#005bbf', light: '#adc7ff', dark: '#004493', contrastText: '#ffffff' },
  secondary: { main: '#006e2b', light: '#93f59e', dark: '#00531f', contrastText: '#ffffff' },
  success: { main: '#188038', light: '#e6f4ea', dark: '#0d652d' },
  warning: { main: '#f9ab00', light: '#fef7e0', dark: '#805600' },
  error: { main: '#ba1a1a', light: '#ffdad6', dark: '#93000a' },
  info: { main: '#005bbf', light: '#d8e2ff', dark: '#004493' },
  background: { default: '#faf9fd', paper: '#ffffff' },
  text: { primary: '#1a1b1e', secondary: '#414754' },
  divider: '#c1c6d6',
}

const darkPalette = {
  mode: 'dark' as const,
  primary: { main: '#adc7ff', light: '#d8e2ff', dark: '#005bc0', contrastText: '#001a41' },
  secondary: { main: '#7adb87', light: '#96f8a1', dark: '#00722d', contrastText: '#002108' },
  success: { main: '#81c784', light: '#2a3b2e', dark: '#66bb6a' },
  warning: { main: '#ffba45', light: '#3a2e10', dark: '#ffa726' },
  error: { main: '#e57373', light: '#3a1f1f', dark: '#ef5350' },
  info: { main: '#adc7ff', light: '#22314f', dark: '#42a5f5' },
  background: { default: '#1a1b1e', paper: '#2f3033' },
  text: { primary: '#f1f0f4', secondary: '#c1c6d6' },
  divider: '#414754',
}

export const theme = createTheme({
  ...baseOptions,
  palette: lightPalette,
})

export const darkTheme = createTheme({
  ...baseOptions,
  palette: darkPalette,
})
