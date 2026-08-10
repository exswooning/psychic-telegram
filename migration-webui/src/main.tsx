import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { ThemeProvider, CssBaseline } from '@mui/material'
import { theme, darkTheme } from '@/theme'
import { useMigrationStore } from '@/store'
import App from '@/App'

const AppThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const darkMode = useMigrationStore((s) => s.darkMode)
  return (
    <ThemeProvider theme={darkMode ? darkTheme : theme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  )
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename="/app">
      <AppThemeProvider>
        <App />
      </AppThemeProvider>
    </BrowserRouter>
  </React.StrictMode>,
)