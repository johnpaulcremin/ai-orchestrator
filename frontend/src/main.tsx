import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './ErrorBoundary.tsx'

// No router dependency for one public page: a /shared/{token} URL (see
// Share.tsx) renders the read-only SharedConversation view instead of the
// main chat App entirely -- checked once at startup, not on every navigation,
// since this app has no other client-side routes to switch between.
const isSharedConversationUrl = /^\/shared\/[^/]+\/?$/.test(window.location.pathname)
const root = createRoot(document.getElementById('root')!)

// Dynamically imported (not a static import like App above) so its
// react-markdown rendering code never lands in the main bundle for the
// overwhelmingly common case -- this branch is only ever hit by an
// anonymous visitor following a share link, never by the app's own users.
if (isSharedConversationUrl) {
  import('./SharedConversation.tsx').then(({ SharedConversation }) => {
    root.render(
      <StrictMode>
        <ErrorBoundary>
          <SharedConversation />
        </ErrorBoundary>
      </StrictMode>,
    )
  })
} else {
  root.render(
    <StrictMode>
      <ErrorBoundary>
        <App />
      </ErrorBoundary>
    </StrictMode>,
  )
}
