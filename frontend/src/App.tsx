import { useEffect, useState } from "react";
import "./App.css";

type Mode = "auto" | "fast" | "smart";

type Conversation = {
  id: number;
  title: string;
  created_at: string;
  updated_at: string;
};

type Message = {
  id: number;
  conversation_id: number;
  role: string;
  content: string;
  mode_used?: string | null;
  notes?: string | null;
  created_at: string;
};

type AskResponse = {
  answer: string;
  mode_used: string;
  notes: string;
};

const API_BASE = "/api";

function App() {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedConversationId, setSelectedConversationId] = useState<number | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [title, setTitle] = useState("New AI Workbench Conversation");
  const [question, setQuestion] = useState("");
  const [mode, setMode] = useState<Mode>("auto");
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState("Ready");

  const selectedConversation =
    conversations.find((conversation) => conversation.id === selectedConversationId) ?? null;

  async function loadConversations(preferredConversationId?: number | null) {
    const res = await fetch(`${API_BASE}/v1/conversations`);
    if (!res.ok) throw new Error("Failed to load conversations");

    const data = (await res.json()) as Conversation[];
    setConversations(data);

    if (preferredConversationId && data.some((item) => item.id === preferredConversationId)) {
      setSelectedConversationId(preferredConversationId);
      return data;
    }

    if (selectedConversationId && data.some((item) => item.id === selectedConversationId)) {
      return data;
    }

    setSelectedConversationId(data.length > 0 ? data[0].id : null);
    return data;
  }

  async function loadMessages(conversationId: number) {
    const res = await fetch(`${API_BASE}/v1/conversations/${conversationId}/messages`);
    if (!res.ok) throw new Error("Failed to load messages");

    const data = (await res.json()) as Message[];
    setMessages(data);
  }

  async function createConversation() {
    setLoading(true);
    setStatus("Creating conversation...");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ title }),
      });

      if (!res.ok) throw new Error("Failed to create conversation");

      const conversation = (await res.json()) as Conversation;
      setSelectedConversationId(conversation.id);
      setTitle("New AI Workbench Conversation");
      await loadConversations(conversation.id);
      await loadMessages(conversation.id);
      setStatus(`Created conversation #${conversation.id}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function renameConversation() {
    if (!selectedConversation) {
      setStatus("Select a conversation first.");
      return;
    }

    const newTitle = window.prompt("Rename conversation:", selectedConversation.title);
    if (!newTitle || !newTitle.trim()) {
      return;
    }

    setLoading(true);
    setStatus("Renaming conversation...");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations/${selectedConversation.id}`, {
        method: "PATCH",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ title: newTitle.trim() }),
      });

      if (!res.ok) throw new Error("Failed to rename conversation");

      await loadConversations(selectedConversation.id);
      setStatus("Conversation renamed.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function deleteConversation() {
    if (!selectedConversation) {
      setStatus("Select a conversation first.");
      return;
    }

    const confirmed = window.confirm(
      `Delete "${selectedConversation.title}"?\n\nThis will permanently delete its saved messages from the local database.`,
    );

    if (!confirmed) {
      return;
    }

    setLoading(true);
    setStatus("Deleting conversation...");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations/${selectedConversation.id}`, {
        method: "DELETE",
      });

      if (!res.ok) throw new Error("Failed to delete conversation");

      setMessages([]);
      setSelectedConversationId(null);
      const updatedConversations = await loadConversations(null);

      if (updatedConversations.length > 0) {
        await loadMessages(updatedConversations[0].id);
      }

      setStatus("Conversation deleted.");
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  async function askQuestion() {
    if (!selectedConversationId) {
      setStatus("Create or select a conversation first.");
      return;
    }

    const cleanQuestion = question.trim();
    if (!cleanQuestion) {
      setStatus("Enter a question first.");
      return;
    }

    setLoading(true);
    setStatus("Asking...");
    setQuestion("");

    try {
      const res = await fetch(`${API_BASE}/v1/conversations/${selectedConversationId}/ask`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          question: cleanQuestion,
          mode,
        }),
      });

      if (!res.ok) throw new Error("Ask request failed");

      const data = (await res.json()) as AskResponse;
      await loadMessages(selectedConversationId);
      await loadConversations(selectedConversationId);
      setStatus(`${data.mode_used} | ${data.notes}`);
    } catch (error) {
      setStatus(error instanceof Error ? error.message : "Unknown error");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    loadConversations().catch((error) => {
      setStatus(error instanceof Error ? error.message : "Backend not reachable");
    });
  }, []);

  useEffect(() => {
    if (selectedConversationId) {
      loadMessages(selectedConversationId).catch((error) => {
        setStatus(error instanceof Error ? error.message : "Failed to load messages");
      });
    } else {
      setMessages([]);
    }
  }, [selectedConversationId]);

  return (
    <main className="app-shell">
      <section className="sidebar">
        <div>
          <h1>AI Workbench</h1>
          <p className="subtitle">Free-first AI orchestration foundation</p>
        </div>

        <div className="create-box">
          <input
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Conversation title"
          />
          <button onClick={createConversation} disabled={loading}>
            Create
          </button>
        </div>

        <div className="conversation-list">
          {conversations.map((conversation) => (
            <button
              key={conversation.id}
              className={conversation.id === selectedConversationId ? "conversation active" : "conversation"}
              onClick={() => setSelectedConversationId(conversation.id)}
            >
              <span>{conversation.title}</span>
              <small>#{conversation.id}</small>
            </button>
          ))}
        </div>
      </section>

      <section className="chat-panel">
        <header className="chat-header">
          <div>
            <h2>{selectedConversation ? selectedConversation.title : "No conversation selected"}</h2>
            <p>{status}</p>
          </div>

          <div className="header-actions">
            <select value={mode} onChange={(event) => setMode(event.target.value as Mode)}>
              <option value="auto">auto</option>
              <option value="fast">fast</option>
              <option value="smart">smart</option>
            </select>

            <button className="secondary-button" onClick={renameConversation} disabled={loading || !selectedConversation}>
              Rename
            </button>

            <button className="danger-button" onClick={deleteConversation} disabled={loading || !selectedConversation}>
              Delete
            </button>
          </div>
        </header>

        <div className="messages">
          {messages.length === 0 ? (
            <div className="empty-state">Create or select a conversation, then ask a question.</div>
          ) : (
            messages.map((message) => (
              <article key={message.id} className={`message ${message.role}`}>
                <div className="message-meta">
                  <strong>{message.role}</strong>
                  {message.mode_used ? <span className="mode-badge">{message.mode_used}</span> : null}
                  <span>{message.created_at}</span>
                </div>
                <p>{message.content}</p>
                {message.notes ? <small>{message.notes}</small> : null}
              </article>
            ))
          )}
        </div>

        <div className="composer">
          <textarea
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            placeholder="Ask inside this saved conversation..."
            onKeyDown={(event) => {
              if (event.key === "Enter" && event.ctrlKey) {
                void askQuestion();
              }
            }}
          />
          <button onClick={askQuestion} disabled={loading}>
            {loading ? "Working..." : "Ask"}
          </button>
        </div>
      </section>
    </main>
  );
}

export default App;