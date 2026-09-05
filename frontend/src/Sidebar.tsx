import { useState, type Dispatch, type ReactNode, type RefObject, type SetStateAction } from "react";
import {
  ArrowLeft,
  Bell,
  BellOff,
  Download,
  HelpCircle,
  Monitor,
  Moon,
  Plus,
  Star,
  Sun,
  Upload,
  Volume2,
  VolumeX,
  X,
} from "lucide-react";
import { Button } from "./Button";
import { HeaderOverflowMenu } from "./HeaderOverflowMenu";
import { formatCost } from "./format";
import type { Conversation, SearchResult } from "./types";

type ThemeValue = "system" | "light" | "dark";
const THEME_CYCLE: Record<ThemeValue, ThemeValue> = { system: "light", light: "dark", dark: "system" };
const THEME_LABEL: Record<ThemeValue, string> = { system: "System", light: "Light", dark: "Dark" };
const THEME_ICON: Record<ThemeValue, typeof Monitor> = { system: Monitor, light: Sun, dark: Moon };

// Wraps every case-insensitive occurrence of `query` in `text` with <mark>,
// so a search result shows exactly what matched, not just a plain snippet.
// Returns `text` unchanged when the query is empty.
function highlightMatch(text: string, query: string): ReactNode {
  const trimmed = query.trim();
  if (!trimmed) {
    return text;
  }
  const escaped = trimmed.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const parts = text.split(new RegExp(`(${escaped})`, "gi"));
  return parts.map((part, index) =>
    index % 2 === 1 ? (
      <mark key={index} className="search-match">
        {part}
      </mark>
    ) : (
      part
    ),
  );
}

type Props = {
  todaySpend: number | null;
  todayCap: number | null;
  todayAvoidedCost: number | null;
  notifyEnabled: boolean;
  toggleNotify: () => void;
  notifySoundEnabled: boolean;
  setNotifySoundEnabled: Dispatch<SetStateAction<boolean>>;
  theme: ThemeValue;
  setTheme: Dispatch<SetStateAction<ThemeValue>>;
  setShortcutsHelpOpen: Dispatch<SetStateAction<boolean>>;
  title: string;
  setTitle: Dispatch<SetStateAction<string>>;
  createConversation: () => Promise<void>;
  busy: boolean;
  importFileInputRef: RefObject<HTMLInputElement | null>;
  importConversation: (fileList: FileList | null) => Promise<void>;
  importing: boolean;
  exportAllConversations: () => Promise<void>;
  exportingAll: boolean;
  previousConversation: Conversation | undefined;
  selectedConversationId: number | null;
  setSelectedConversationId: Dispatch<SetStateAction<number | null>>;
  searchInputRef: RefObject<HTMLInputElement | null>;
  searchQuery: string;
  setSearchQuery: Dispatch<SetStateAction<string>>;
  setSearchResults: Dispatch<SetStateAction<SearchResult[]>>;
  setSearching: Dispatch<SetStateAction<boolean>>;
  isMac: boolean;
  allTags: string[];
  tagFilter: string;
  setTagFilter: Dispatch<SetStateAction<string>>;
  sortOrder: "recent" | "name";
  setSortOrder: Dispatch<SetStateAction<"recent" | "name">>;
  showArchived: boolean;
  toggleShowArchived: () => Promise<void>;
  favoritesOnly: boolean;
  setFavoritesOnly: Dispatch<SetStateAction<boolean>>;
  bulkSelectMode: boolean;
  toggleBulkSelectMode: () => void;
  bulkSelectedIds: Set<number>;
  exportSelectedConversations: () => Promise<void>;
  exportingSelected: boolean;
  bulkTagSelected: () => Promise<void>;
  bulkWorking: boolean;
  bulkArchiveSelected: () => Promise<void>;
  bulkDeleteSelected: () => Promise<void>;
  searching: boolean;
  searchResults: SearchResult[];
  selectSearchResult: (conversationId: number) => void;
  visibleConversations: Conversation[];
  toggleBulkSelected: (conversationId: number) => void;
  toggleFavorite: (conversation: Conversation) => Promise<void>;
  jwtEnabled: boolean;
  me: string | null;
  logout: () => void;
  loginUsername: string;
  setLoginUsername: Dispatch<SetStateAction<string>>;
  loginPassword: string;
  setLoginPassword: Dispatch<SetStateAction<string>>;
  submitAuth: (register: boolean) => Promise<void>;
  authBusy: boolean;
  registrationAllowed: boolean;
  authMessage: string | null;
  usernameInputRef: RefObject<HTMLInputElement | null>;
  authEnabled: boolean;
  token: string;
  setToken: Dispatch<SetStateAction<string>>;
  tokenInputRef: RefObject<HTMLInputElement | null>;
  // Off-canvas drawer state on mobile (see App.css's ~850px breakpoint) --
  // both no-ops above that width, where the sidebar is a permanently
  // visible grid column regardless of `mobileOpen`.
  mobileOpen: boolean;
  onCloseMobile: () => void;
};

export function Sidebar({
  todaySpend,
  todayCap,
  todayAvoidedCost,
  notifyEnabled,
  toggleNotify,
  notifySoundEnabled,
  setNotifySoundEnabled,
  theme,
  setTheme,
  setShortcutsHelpOpen,
  title,
  setTitle,
  createConversation,
  busy,
  importFileInputRef,
  importConversation,
  importing,
  exportAllConversations,
  exportingAll,
  previousConversation,
  selectedConversationId,
  setSelectedConversationId,
  searchInputRef,
  searchQuery,
  setSearchQuery,
  setSearchResults,
  setSearching,
  isMac,
  allTags,
  tagFilter,
  setTagFilter,
  sortOrder,
  setSortOrder,
  showArchived,
  toggleShowArchived,
  favoritesOnly,
  setFavoritesOnly,
  bulkSelectMode,
  toggleBulkSelectMode,
  bulkSelectedIds,
  exportSelectedConversations,
  exportingSelected,
  bulkTagSelected,
  bulkWorking,
  bulkArchiveSelected,
  bulkDeleteSelected,
  searching,
  searchResults,
  selectSearchResult,
  visibleConversations,
  toggleBulkSelected,
  toggleFavorite,
  jwtEnabled,
  me,
  logout,
  loginUsername,
  setLoginUsername,
  loginPassword,
  setLoginPassword,
  submitAuth,
  authBusy,
  registrationAllowed,
  authMessage,
  usernameInputRef,
  authEnabled,
  token,
  setToken,
  tokenInputRef,
  mobileOpen,
  onCloseMobile,
}: Props) {
  // Both purely local disclosure toggles -- nothing outside this component
  // needs to know whether either is open (unlike the header's own
  // HeaderOverflowMenu, whose open state affects the app-wide keyboard
  // shortcut suppression list in App.tsx).
  const [newConversationOpen, setNewConversationOpen] = useState(false);
  const [sidebarMenuOpen, setSidebarMenuOpen] = useState(false);

  return (
    <section className={`sidebar${mobileOpen ? " sidebar-open" : ""}`}>
      <div className="sidebar-title-row">
        <div>
          <h1>AI Workbench</h1>
        </div>
        <Button
          iconOnly
          size="sm"
          variant="ghost"
          className="sidebar-close-button"
          onClick={onCloseMobile}
          aria-label="Close conversation list"
          title="Close"
          icon={<X size={18} />}
        />
        {todaySpend !== null ? (
          <span
            className="spend-indicator"
            title={
              todayCap !== null
                ? `Your own spend today, out of your ${formatCost(todayCap) || "$0.00"} daily cap`
                : "Your own spend today"
            }
          >
            💰 {formatCost(todaySpend) || "$0.00"}
            {todayCap !== null ? ` / ${formatCost(todayCap) || "$0.00"}` : ""} today
          </span>
        ) : null}
        {todayAvoidedCost !== null && todayAvoidedCost > 0 ? (
          <span
            className="spend-indicator saved-indicator"
            title="Spend the response cache avoided today — a repeated question served from cache instead of calling a model again"
          >
            🛟 {formatCost(todayAvoidedCost) || "$0.00"} saved today
          </span>
        ) : null}
        <Button
          iconOnly
          size="sm"
          variant="ghost"
          className={`notify-toggle${notifyEnabled ? " active" : ""}`}
          onClick={toggleNotify}
          aria-label={
            notifyEnabled
              ? "Background reply notifications on. Click to turn off."
              : "Background reply notifications off. Click to turn on."
          }
          title="Notify me when a reply finishes while this tab is in the background"
          icon={notifyEnabled ? <Bell size={16} /> : <BellOff size={16} />}
        />
        {notifyEnabled && (
          <Button
            iconOnly
            size="sm"
            variant="ghost"
            className={`notify-sound-toggle${notifySoundEnabled ? " active" : ""}`}
            onClick={() => setNotifySoundEnabled((current) => !current)}
            aria-label={
              notifySoundEnabled
                ? "Notification sound on. Click to turn off."
                : "Notification sound off. Click to turn on."
            }
            title="Play a sound with background reply notifications"
            icon={notifySoundEnabled ? <Volume2 size={16} /> : <VolumeX size={16} />}
          />
        )}
        <Button
          size="sm"
          variant="ghost"
          className="theme-toggle"
          onClick={() => setTheme((current) => THEME_CYCLE[current])}
          aria-label={`Theme: ${THEME_LABEL[theme]}. Click to switch to ${THEME_LABEL[THEME_CYCLE[theme]]}.`}
          title="Cycle theme (system / light / dark)"
          icon={
            (() => {
              const Icon = THEME_ICON[theme];
              return <Icon size={16} />;
            })()
          }
        >
          {THEME_LABEL[theme]}
        </Button>
        <Button
          iconOnly
          size="sm"
          variant="ghost"
          className="shortcuts-help-toggle"
          onClick={() => setShortcutsHelpOpen(true)}
          aria-label="Keyboard shortcuts"
          title="Show keyboard shortcuts (?)"
          icon={<HelpCircle size={16} />}
        />
        <Button
          type="button"
          className="cost-legend"
          aria-label="What does the $ marker mean?"
          title="$ = this action uses paid API tokens/credits."
        >
          $
        </Button>
      </div>

      <div className="sidebar-primary-actions">
        <div className="new-conversation-control">
          <Button
            size="sm"
            onClick={() => setNewConversationOpen((current) => !current)}
            aria-expanded={newConversationOpen}
            title={`New conversation (${isMac ? "⌥N" : "Alt+N"} creates one immediately with the current title)`}
            icon={<Plus size={16} />}
          >
            New conversation
          </Button>
          {newConversationOpen ? (
            <div className="new-conversation-popover" role="dialog" aria-label="New conversation">
              <input
                value={title}
                onChange={(event) => setTitle(event.target.value)}
                placeholder="New AI Workbench Conversation"
                aria-label="New conversation title"
                autoFocus
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void createConversation();
                    setNewConversationOpen(false);
                  } else if (event.key === "Escape") {
                    setNewConversationOpen(false);
                  }
                }}
              />
              <div className="new-conversation-popover-actions">
                <Button
                  size="sm"
                  variant="primary"
                  disabled={busy}
                  onClick={() => {
                    void createConversation();
                    setNewConversationOpen(false);
                  }}
                >
                  Create
                </Button>
                <Button size="sm" variant="ghost" onClick={() => setNewConversationOpen(false)}>
                  Cancel
                </Button>
              </div>
            </div>
          ) : null}
        </div>

        <input
          ref={importFileInputRef}
          type="file"
          accept="application/json"
          className="visually-hidden"
          aria-label="Import a conversation or an export-all bundle from a JSON file"
          onChange={(event) => {
            void importConversation(event.target.files);
            event.target.value = "";
          }}
        />
        <HeaderOverflowMenu
          open={sidebarMenuOpen}
          onOpenChange={setSidebarMenuOpen}
          triggerLabel="More conversation-list actions"
        >
          <Button
            role="menuitem"
            size="sm"
            onClick={() => {
              importFileInputRef.current?.click();
              setSidebarMenuOpen(false);
            }}
            disabled={importing}
            title="Accepts a single-conversation export, or a whole Export all bundle"
            icon={<Upload size={16} />}
          >
            {importing ? "Importing…" : "Import conversation"}
          </Button>
          <Button
            role="menuitem"
            size="sm"
            onClick={() => {
              void exportAllConversations();
              setSidebarMenuOpen(false);
            }}
            disabled={exportingAll}
            icon={<Download size={16} />}
          >
            {exportingAll ? "Exporting…" : "Export all"}
          </Button>
          <label className="header-overflow-checkbox">
            <input
              type="checkbox"
              checked={showArchived}
              onChange={() => void toggleShowArchived()}
              aria-label="Show archived conversations"
            />
            Show archived
          </label>
          <label className="header-overflow-checkbox">
            <input
              type="checkbox"
              checked={favoritesOnly}
              onChange={() => setFavoritesOnly((current) => !current)}
              aria-label="Favorites only"
            />
            ★ Favorites only
          </label>
        </HeaderOverflowMenu>
      </div>

      {previousConversation && previousConversation.id !== selectedConversationId ? (
        <Button
          type="button"
          className="back-to-previous"
          onClick={() => setSelectedConversationId(previousConversation.id)}
          title="Switch back to the conversation you were just in"
          icon={<ArrowLeft size={16} />}
        >
          Back to "{previousConversation.title}"
        </Button>
      ) : null}

      <div className="search-box">
        <input
          ref={searchInputRef}
          value={searchQuery}
          onChange={(event) => {
            const value = event.target.value;
            setSearchQuery(value);
            if (!value.trim()) {
              setSearchResults([]);
              setSearching(false);
            }
          }}
          placeholder={`Search conversations… (${isMac ? "⌘K" : "Ctrl+K"})`}
          aria-label="Search conversations"
          type="search"
        />
      </div>

      {allTags.length > 0 && (
        <select
          className="tag-filter"
          value={tagFilter}
          onChange={(event) => setTagFilter(event.target.value)}
          aria-label="Filter conversations by tag"
        >
          <option value="">All tags</option>
          {allTags.map((tag) => (
            <option key={tag} value={tag}>
              {tag}
            </option>
          ))}
        </select>
      )}

      <select
        className="tag-filter"
        value={sortOrder}
        onChange={(event) => setSortOrder(event.target.value === "name" ? "name" : "recent")}
        aria-label="Sort conversations"
      >
        <option value="recent">Sort: Recent</option>
        <option value="name">Sort: Name (A-Z)</option>
      </select>

      <div className="show-archived-toggle-row">
        <Button type="button" className="select-mode-toggle" onClick={toggleBulkSelectMode}>
          {bulkSelectMode ? "Cancel select" : "Select"}
        </Button>
      </div>

      {bulkSelectMode && (
        <div className="bulk-action-bar">
          <span>{bulkSelectedIds.size} selected</span>
          <Button
            type="button"
            onClick={() => void exportSelectedConversations()}
            disabled={bulkSelectedIds.size === 0 || exportingSelected}
          >
            {exportingSelected ? "Exporting…" : "Export selected"}
          </Button>
          <Button
            type="button"
            onClick={() => void bulkTagSelected()}
            disabled={bulkSelectedIds.size === 0 || bulkWorking}
          >
            Add tag
          </Button>
          <Button
            type="button"
            onClick={() => void bulkArchiveSelected()}
            disabled={bulkSelectedIds.size === 0 || bulkWorking}
          >
            Archive selected
          </Button>
          <Button
            type="button"
            variant="danger"
            onClick={() => void bulkDeleteSelected()}
            disabled={bulkSelectedIds.size === 0 || bulkWorking}
          >
            Delete selected
          </Button>
        </div>
      )}

      {searchQuery.trim() ? (
        <div className="conversation-list search-results">
          {searching ? (
            <div className="empty-state small">Searching…</div>
          ) : searchResults.length === 0 ? (
            <div className="empty-state small">No matches.</div>
          ) : (
            searchResults.map((result) => (
              // Left raw: a multi-line list row -- .btn-sm's fixed 32px height would break it.
              <button
                key={result.id}
                className={
                  result.id === selectedConversationId ? "conversation active" : "conversation"
                }
                onClick={() => selectSearchResult(result.id)}
              >
                <span>{highlightMatch(result.title, searchQuery)}</span>
                <small className="search-snippet">
                  {highlightMatch(
                    result.snippet.length > 140
                      ? `${result.snippet.slice(0, 140)}…`
                      : result.snippet,
                    searchQuery,
                  )}
                </small>
              </button>
            ))
          )}
        </div>
      ) : (
        <div className="conversation-list">
          {visibleConversations.map((conversation, conversationIndex) => (
            <div key={conversation.id} className="conversation-row">
              {bulkSelectMode && (
                <input
                  type="checkbox"
                  className="bulk-select-checkbox"
                  checked={bulkSelectedIds.has(conversation.id)}
                  onChange={() => toggleBulkSelected(conversation.id)}
                  aria-label={`Select "${conversation.title}"`}
                />
              )}
              {/* Left raw: a multi-line list row -- .btn-sm's fixed 32px height would break it. */}
              <button
                data-conversation-id={conversation.id}
                className={conversation.id === selectedConversationId ? "conversation active" : "conversation"}
                title={conversation.title}
                aria-label={
                  `${conversation.title}${conversation.archived ? " (archived)" : ""}` +
                  (conversation.message_count
                    ? `, ${conversation.message_count} message${conversation.message_count === 1 ? "" : "s"}`
                    : "")
                }
                onClick={() => setSelectedConversationId(conversation.id)}
                onKeyDown={(event) => {
                  // Arrow/Home/End navigate the list like a listbox — Enter/Space
                  // already select via the button's native click activation.
                  if (
                    event.key !== "ArrowDown" &&
                    event.key !== "ArrowUp" &&
                    event.key !== "Home" &&
                    event.key !== "End"
                  ) {
                    return;
                  }
                  event.preventDefault();
                  const lastIndex = visibleConversations.length - 1;
                  const targetIndex =
                    event.key === "ArrowDown"
                      ? Math.min(conversationIndex + 1, lastIndex)
                      : event.key === "ArrowUp"
                        ? Math.max(conversationIndex - 1, 0)
                        : event.key === "Home"
                          ? 0
                          : lastIndex;
                  const target = visibleConversations[targetIndex];
                  if (!target) {
                    return;
                  }
                  setSelectedConversationId(target.id);
                  // No deferral needed — every row is always rendered
                  // (selection only toggles the "active" class), so the
                  // target button already exists in the DOM right now.
                  document
                    .querySelector<HTMLButtonElement>(`[data-conversation-id="${target.id}"]`)
                    ?.focus();
                }}
              >
                <span className="conversation-title">
                  {conversation.title}
                  {conversation.archived ? <small className="archived-tag"> (archived)</small> : null}
                </span>
                <span className="conversation-meta">
                  {conversation.tags && conversation.tags.length > 0 ? (
                    <span className="conversation-tags">
                      {conversation.tags.map((tag) => (
                        <small key={tag} className="tag-chip">
                          {tag}
                        </small>
                      ))}
                    </span>
                  ) : null}
                  <small>#{conversation.id}</small>
                  {conversation.message_count ? (
                    <small
                      className="message-count-badge"
                      title={`${conversation.message_count} message${conversation.message_count === 1 ? "" : "s"}`}
                    >
                      {conversation.message_count}
                    </small>
                  ) : null}
                </span>
              </button>
              <Button
                iconOnly
                size="sm"
                variant="ghost"
                type="button"
                className={conversation.favorite ? "favorite-star active" : "favorite-star"}
                onClick={() => void toggleFavorite(conversation)}
                aria-label={
                  conversation.favorite
                    ? `Unfavorite "${conversation.title}"`
                    : `Favorite "${conversation.title}"`
                }
                aria-pressed={Boolean(conversation.favorite)}
                title={conversation.favorite ? "Unfavorite" : "Favorite"}
                icon={<Star size={16} fill={conversation.favorite ? "currentColor" : "none"} />}
              />
            </div>
          ))}
        </div>
      )}

      <div className="sidebar-footer">
        {jwtEnabled ? (
          me ? (
            <div className="auth-signed-in">
              <span>
                Signed in as <strong>{me}</strong>
              </span>
              <Button onClick={logout}>
                Log out
              </Button>
            </div>
          ) : (
            <div className="auth-form">
              {/* A heading, not a <label> — "Sign in" describes the whole
                  mini-form, not one specific field, and each input below
                  already carries its own aria-label. An unassociated
                  <label> announced nothing useful to screen readers. */}
              <h3 className="auth-form-heading">Sign in</h3>
              <input
                ref={usernameInputRef}
                value={loginUsername}
                onChange={(event) => setLoginUsername(event.target.value)}
                placeholder="username"
                aria-label="Username"
                autoComplete="username"
              />
              <input
                type="password"
                value={loginPassword}
                onChange={(event) => setLoginPassword(event.target.value)}
                placeholder="password"
                aria-label="Password"
                autoComplete="current-password"
                onKeyDown={(event) => {
                  if (event.key === "Enter" && !event.nativeEvent.isComposing) {
                    event.preventDefault();
                    void submitAuth(false);
                  }
                }}
              />
              <div className="auth-buttons">
                <Button variant="primary" onClick={() => submitAuth(false)} disabled={authBusy}>
                  Log in
                </Button>
                {registrationAllowed ? (
                  <Button onClick={() => submitAuth(true)} disabled={authBusy}>
                    Register
                  </Button>
                ) : null}
              </div>
              {authMessage ? (
                <p role="alert" className="auth-message">
                  {authMessage}
                </p>
              ) : null}
            </div>
          )
        ) : (
          <>
            <label htmlFor="api-token">API token{authEnabled ? "" : " (optional)"}</label>
            <input
              ref={tokenInputRef}
              id="api-token"
              type="password"
              value={token}
              onChange={(event) => setToken(event.target.value)}
              placeholder="Bearer token"
              autoComplete="off"
            />
          </>
        )}
      </div>
    </section>
  );
}
