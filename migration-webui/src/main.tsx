import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import App from '@/App'
import { appBase } from '@/workspace'
import { AppThemeProvider } from '@/AppThemeProvider'
import { installMigrationHeader } from '@/api/migration'

installMigrationHeader()

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter basename={appBase}>
      <AppThemeProvider>
        <App />
      </AppThemeProvider>
    </BrowserRouter>
  </React.StrictMode>,
)