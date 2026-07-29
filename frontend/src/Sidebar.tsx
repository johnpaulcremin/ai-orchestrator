import type { Dispatch, ReactNode, RefObject, SetStateAction } from "react";
import { formatCost } from "./format";
import type { Conversation, SearchResult } from "./types";

type ThemeValue = "system" | "light" | "dark";
const THEME_CYCLE: Record<ThemeValue, ThemeValue> = { system: "light", light: "dark", dark: "system" };
const THEME_LABEL: Record<ThemeValue, string> = { system: "🖥️ System", light: "☀️ Light", dark: "🌙 Dark" };

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
}: Props) {
  return (
    <section className="sidebar">
      <div className="sidebar-title-row">
        <div>
          <h1>AI Workbench</h1>
          <p className="subtitle">Free-first AI orchestration foundation</p>
        </div>
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
        <button
          type="button"
          className={`secondary-button notify-toggle${notifyEnabled ? " active" : ""}`}
          onClick={toggleNotify}
          aria-label={
            notifyEnabled
              ? "Background reply notifications on. Click to turn off."
              : "Background reply notifications off. Click to turn on."
          }
          title="Notify me when a reply finishes while this tab is in the background"
        >
          {notifyEnabled ? "🔔" : "🔕"}
        </button>
        {notifyEnabled && (
          <button
            type="button"
            className={`secondary-button notify-sound-toggle${notifySoundEnabled ? " active" : ""}`}
            onClick={() => setNotifySoundEnabled((current) => !current)}
            aria-label={
              notifySoundEnabled
                ? "Notification sound on. Click to turn off."
                : "Notification sound off. Click to turn on."
            }
            title="Play a sound with background reply notifications"
          >
            {notifySoundEnabled ? "🔊" : "🔈"}
          </button>
        )}
        <button
          type="button"
          className="secondary-button theme-toggle"
          onClick={() => setTheme((current) => THEME_CYCLE[current])}
          aria-label={`Theme: ${THEME_LABEL[theme]}. Click to switch to ${THEME_LABEL[THEME_CYCLE[theme]]}.`}
          title="Cycle theme (system / light / dark)"
        >
          {THEME_LABEL[theme]}
        </button>
        <button
          type="button"
          className="secondary-button shortcuts-help-toggle"
          onClick={() => setShortcutsHelpOpen(true)}
          aria-label="Keyboard shortcuts"
          title="Show keyboard shortcuts (?)"
        >
          ❓
        </button>
        <button
          type="button"
          className="secondary-button cost-legend"
          aria-label="What does the $ marker mean?"
          title="$ = this action uses paid API tokens/credits."
        >
          $
        </button>
      </div>

      <div className="create-box">
        <input
          value={title}
          onChange={(event) => setTitle(event.target.value)}
          placeholder={`Conversation title (${isMac ? "⌥N" : "Alt+N"})`}
          aria-label="New conversation title"
        />
        <button onClick={createConversation} disabled={busy}>
          Create
        </button>
      </div>

      <div className="import-box">
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
        <button
          type="button"
          className="secondary-button"
          onClick={() => importFileInputRef.current?.click()}
          disabled={importing}
          title="Accepts a single-conversation export, or a whole Export all bundle"
        >
          {importing ? "Importing…" : "⬆️ Import conversation"}
        </button>
        <button
          type="button"
          className="secondary-button"
          onClick={() => void exportAllConversations()}
          disabled={exportingAll}
        >
          {exportingAll ? "Exporting…" : "⬇️ Export all"}
        </button>
      </div>

      {previousConversation && previousConversation.id !== selectedConversationId ? (
        <button
          type="button"
          className="secondary-button back-to-previous"
          onClick={() => setSelectedConversationId(previousConversation.id)}
          title="Switch back to the conversation you were just in"
        >
          ← Back to "{previousConversation.title}"
        </button>
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
        <label className="show-archived-toggle">
          <input
            type="checkbox"
            checked={showArchived}
            onChange={() => void toggleShowArchived()}
          />
          Show archived
        </label>
        <label className="show-archived-toggle">
          <input
            type="checkbox"
            checked={favoritesOnly}
            onChange={() => setFavoritesOnly((current) => !current)}
          />
          ★ Favorites only
        </label>
        <button type="button" className="secondary-button select-mode-toggle" onClick={toggleBulkSelectMode}>
          {bulkSelectMode ? "Cancel select" : "Select"}
        </button>
      </div>

      {bulkSelectMode && (
        <div className="bulk-action-bar">
          <span>{bulkSelectedIds.size} selected</span>
          <button
            type="button"
            className="secondary-button"
            onClick={() => void exportSelectedConversations()}
            disabled={bulkSelectedIds.size === 0 || exportingSelected}
          >
            {exportingSelected ? "Exporting…" : "Export selected"}
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() => void bulkTagSelected()}
            disabled={bulkSelectedIds.size === 0 || bulkWorking}
          >
            Add tag
          </button>
          <button
            type="button"
            className="secondary-button"
            onClick={() => void bulkArchiveSelected()}
            disabled={bulkSelectedIds.size === 0 || bulkWorking}
          >
            Archive selected
          </button>
          <button
            type="button"
            className="danger-button"
            onClick={() => void bulkDeleteSelected()}
            disabled={bulkSelectedIds.size === 0 || bulkWorking}
          >
            Delete selected
          </button>
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
              <button
                data-conversation-id={conversation.id}
                className={conversation.id === selectedConversationId ? "conversation active" : "conversation"}
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
                <span>
                  {conversation.title}
                  {conversation.archived ? <small className="archived-tag"> (archived)</small> : null}
                </span>
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
              </button>
              <button
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
              >
                {conversation.favorite ? "★" : "☆"}
              </button>
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
              <button className="secondary-button" onClick={logout}>
                Log out
              </button>
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
                <button onClick={() => submitAuth(false)} disabled={authBusy}>
                  Log in
                </button>
                {registrationAllowed ? (
                  <button
                    className="secondary-button"
                    onClick={() => submitAuth(true)}
                    disabled={authBusy}
                  >
                    Register
                  </button>
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
