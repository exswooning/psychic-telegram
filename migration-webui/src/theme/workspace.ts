import { createTheme } from '@mui/material/styles'
import { theme, darkTheme } from './index'
import { workspaceMotion } from './workspaceMotion'
export const workspaceTheme = (dark: boolean) => createTheme(dark ? darkTheme : theme, {
  palette: {
    primary: dark ? { main: '#a8c7fa', light: '#253858', dark: '#d3e3fd', contrastText: '#062e6f' } : { main: '#0b57d0', light: '#d3e3fd', dark: '#0842a0', contrastText: '#fff' },
    background: dark ? { default: '#14171c', paper: '#1e2229' } : { default: '#f8fafd', paper: '#ffffff' },
    text: dark ? { primary: '#e3e6ed', secondary: '#b2bac9' } : { primary: '#1f1f1f', secondary: '#444746' },
    divider: dark ? '#3d444f' : '#dde3ec',
  },
  shape: { borderRadius: 12 },
  typography: {
    h2: { fontSize: '1.875rem', fontWeight: 400, letterSpacing: '-.5px' },
    h3: { fontSize: '1.6rem', fontWeight: 400 },
    h4: { fontWeight: 400, fontSize: '1.75rem' },
    h5: { fontWeight: 400, fontSize: '1.25rem' },
    h6: { fontWeight: 500, fontSize: '1rem' },
    body2: { fontSize: '.875rem', lineHeight: 1.6 },
    caption: { fontSize: '.75rem', lineHeight: 1.5 },
  },
  components: {
    MuiCard: { styleOverrides: { root: { borderRadius: 16, borderColor: dark ? '#3d444f' : '#dde3ec' } } },
    MuiPaper: { styleOverrides: { root: { backgroundImage: 'none' }, rounded: { borderRadius: 16 } } },
    MuiTableCell: { styleOverrides: { root: { padding: '15px 18px', borderColor: dark ? '#3d444f' : '#e8edf3' }, head: { color: dark ? '#b2bac9' : '#5e6571', backgroundColor: dark ? '#242a33' : '#f8fafd', fontWeight: 500 } } },
    MuiButton: { styleOverrides: { root: { letterSpacing: 0, minHeight: 40, borderRadius: 24, padding: '9px 20px' }, contained: { boxShadow: 'none' } } },
    MuiOutlinedInput: { styleOverrides: { root: { borderRadius: 8 } } },
    MuiTabs: { styleOverrides: { root: { minHeight: 48 }, indicator: { height: 3, borderRadius: '3px 3px 0 0' } } },
    MuiTab: { styleOverrides: { root: { textTransform: 'none', fontWeight: 500, minHeight: 48 } } },
    MuiDrawer: { defaultProps: { transitionDuration: { enter: 280, exit: 200 } } },
    MuiDialog: { styleOverrides: { paper: { borderRadius: 24 } } },
    MuiAlert: { styleOverrides: { root: { borderRadius: 12 } } },
    MuiCssBaseline: { styleOverrides: {
      '*:focus-visible': { outline: '2px solid #4285f4', outlineOffset: 3 },
      'html': { scrollPaddingTop: 150 },
      'body': { fontSynthesis: 'none' },
      'button, a, input': { WebkitTapHighlightColor: 'transparent' },
      ...workspaceMotion,
    } },
  },
})
