import React from 'react'
import { ThemeProvider, CssBaseline } from '@mui/material'
import { theme, darkTheme } from '@/theme'
import { useMigrationStore } from '@/store'

export const AppThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const darkMode = useMigrationStore((s) => s.darkMode)
  return (
    <ThemeProvider theme={darkMode ? darkTheme : theme}>
      <CssBaseline />
      {children}
    </ThemeProvider>
  )
}
