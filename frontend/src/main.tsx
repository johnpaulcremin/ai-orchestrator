import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { ErrorBoundary } from './ErrorBoundary.tsx'
import { installCrashReporter } from './crashReporter.ts'

// Install the window error handlers before we render, so a device that only
// shows a blank page (e.g. a phone, devtools out of reach) still leaves a
// readable error server-side. See crashReporter.ts.
//
// This catches render-time and runtime errors — the overwhelmingly common
// blank-page cause. It CANNOT catch a throw during module evaluation of the
// static imports above (ES import hoisting runs their whole graph before
// this line): the only thing that could is an inline <script> in index.html
// running before the bundle loads, which this app's own CSP (script-src
// 'self', no unsafe-inline — see frontend/nginx.conf / security_headers.py)
// deliberately forbids. That tradeoff is accepted here.
installCrashReporter()

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
