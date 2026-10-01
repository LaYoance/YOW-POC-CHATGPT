/**
 * ChatGPT Integration POC - Vanilla JS Reactive Frontend
 */

const state = {
  session: null,
  projects: [],
  conversations: [],
  activeConversationId: null,
  activeProjectId: null,
  activeBranchMessages: [],
  isStreaming: false,
};

// DOM Elements
const sessionBadge = document.getElementById("session-badge");
const projectsList = document.getElementById("projects-list");
const conversationsList = document.getElementById("conversations-list");
const messagesContainer = document.getElementById("messages-container");
const chatInput = document.getElementById("chat-input");
const btnSend = document.getElementById("btn-send");
const btnNewChat = document.getElementById("btn-new-chat");
const btnSyncProjects = document.getElementById("btn-sync-projects");
const btnSyncConvs = document.getElementById("btn-sync-convs");
const transportSelect = document.getElementById("transport-select");
const activeTitle = document.getElementById("active-title");
const activeProjectTag = document.getElementById("active-project-tag");

// Modal Elements
const sessionModal = document.getElementById("session-modal");
const btnSessionModal = document.getElementById("btn-session-modal");
const btnCloseModal = document.getElementById("btn-close-modal");
const btnImportCookies = document.getElementById("btn-import-cookies");
const btnReconnectWizard = document.getElementById("btn-reconnect-wizard");
const btnSyncBrowser = document.getElementById("btn-sync-browser");
const cookiesInput = document.getElementById("cookies-input");
const modalSessionStatus = document.getElementById("modal-session-status");
const modalSessionEmail = document.getElementById("modal-session-email");

// Initialize
document.addEventListener("DOMContentLoaded", async () => {
  setupEventListeners();
  await loadSessionStatus();
  await loadProjects();
  await loadConversations();
  await loadTransportStatus();
});

function setupEventListeners() {
  btnSessionModal.addEventListener("click", () => sessionModal.classList.remove("hidden"));
  sessionBadge.addEventListener("click", () => sessionModal.classList.remove("hidden"));
  btnCloseModal.addEventListener("click", () => sessionModal.classList.remove("hidden") || sessionModal.classList.add("hidden"));

  btnImportCookies.addEventListener("click", async () => {
    const raw = cookiesInput.value.trim();
    if (!raw) return;
    try {
      const res = await fetch("/api/session/import", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ cookies: raw })
      });
      const data = await res.json();
      alert("Session importée : " + data.status + " - " + data.message);
      sessionModal.classList.add("hidden");
      await loadSessionStatus();
    } catch (e) {
      alert("Erreur import: " + e.message);
    }
  });

  btnReconnectWizard.addEventListener("click", async () => {
    try {
      btnReconnectWizard.disabled = true;
      btnReconnectWizard.textContent = "Lancement...";
      modalSessionStatus.textContent = "Ouverture du navigateur Chromium... Veuillez patienter.";

      const res = await fetch("/api/session/reconnect", { method: "POST" });
      const data = await res.json();
      modalSessionStatus.textContent = data.message || "Chromium ouvert. Connectez-vous sur chatgpt.com.";
      pollReconnectWizard();
    } catch (e) {
      modalSessionStatus.textContent = "Erreur de lancement : " + e.message;
    } finally {
      btnReconnectWizard.disabled = false;
      btnReconnectWizard.textContent = "Lancer Reconnexion Navigateur";
    }
  });

  btnSyncBrowser.addEventListener("click", async () => {
    try {
      btnSyncBrowser.disabled = true;
      btnSyncBrowser.textContent = "Synchronisation...";
      modalSessionStatus.textContent = "Lecture des cookies du profil Chromium...";

      const res = await fetch("/api/session/sync-browser", { method: "POST" });
      const data = await res.json();
      modalSessionStatus.textContent = data.status + " : " + (data.message || data.message_fr);
      if (data.status === "VALID" || data.is_valid) {
        await loadSessionStatus();
        setTimeout(() => sessionModal.classList.add("hidden"), 1200);
      }
    } catch (e) {
      modalSessionStatus.textContent = "Erreur sync: " + e.message;
    } finally {
      btnSyncBrowser.disabled = false;
      btnSyncBrowser.textContent = "Synchroniser session existante";
    }
  });

  btnSyncProjects.addEventListener("click", async () => {
    btnSyncProjects.textContent = "⌛";
    try {
      await fetch("/api/projects/sync", { method: "POST" });
      await loadProjects();
    } finally {
      btnSyncProjects.textContent = "↻";
    }
  });

  btnSyncConvs.addEventListener("click", async () => {
    btnSyncConvs.textContent = "⌛";
    try {
      await fetch("/api/chat/poll-site", { method: "POST" });
      await loadConversations();
      if (state.activeConversationId) {
        await selectConversation(state.activeConversationId, true);
      }
    } finally {
      btnSyncConvs.textContent = "↻";
    }
  });

  btnNewChat.addEventListener("click", () => {
    state.activeConversationId = null;
    activeTitle.textContent = "Nouvelle conversation";
    messagesContainer.innerHTML = '<div style="text-align: center; color: var(--text-muted); margin-top: 100px;">Nouvelle conversation prête. Posez votre question.</div>';
    document.querySelectorAll(".item-row").forEach(el => el.classList.remove("active"));
  });

  btnSend.addEventListener("click", handleSendMessage);
  chatInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSendMessage();
    }
  });

  transportSelect.addEventListener("change", async (e) => {
    const val = e.target.value;
    const mode = val === "AUTO" ? null : val;
    await fetch("/api/transport/mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode })
    });
  });
}

async function loadSessionStatus() {
  try {
    const res = await fetch("/api/session/status");
    const data = await res.json();
    state.session = data;

    sessionBadge.textContent = data.status;
    sessionBadge.className = "badge " + (
      data.status === "VALID" ? "badge-valid" :
      data.status === "EXPIRED" ? "badge-expired" : "badge-warning"
    );

    modalSessionStatus.textContent = data.status + " (" + data.message + ")";
    modalSessionEmail.textContent = data.user_email ? "Utilisateur : " + data.user_email : "";
  } catch (e) {
    sessionBadge.textContent = "ERR";
    sessionBadge.className = "badge badge-expired";
  }
}

async function pollReconnectWizard() {
  const timer = setInterval(async () => {
    try {
      const res = await fetch("/api/session/reconnect/status");
      const data = await res.json();
      const status = data.wizard_status || data.status;

      if (status === "RUNNING") {
        modalSessionStatus.textContent = `${data.message} (${data.elapsed_seconds}s / ${data.timeout_seconds}s)`;
      } else if (status === "SUCCESS" || status === "COMPLETED") {
        clearInterval(timer);
        modalSessionStatus.textContent = "Reconnexion réussie ! Session synchronisée.";
        await loadSessionStatus();
        setTimeout(() => sessionModal.classList.add("hidden"), 1500);
      } else if (status === "FAILED" || status === "TIMEOUT" || status === "CANCELLED") {
        clearInterval(timer);
        modalSessionStatus.textContent = "Fin assistant : " + data.message;
      }
    } catch (e) {
      clearInterval(timer);
    }
  }, 1500);
}

async function loadTransportStatus() {
  try {
    const res = await fetch("/api/transport/status");
    const data = await res.json();
    if (data.forced_mode) {
      transportSelect.value = data.forced_mode;
    } else {
      transportSelect.value = "AUTO";
    }
  } catch (e) {
    console.error("Transport status error", e);
  }
}

async function loadProjects() {
  try {
    const res = await fetch("/api/projects");
    const data = await res.json();
    state.projects = data.items || [];

    if (state.projects.length === 0) {
      projectsList.innerHTML = '<div class="item-meta" style="padding: 4px 8px;">Aucun projet (cliquez sur ↻ pour synchroniser)</div>';
      return;
    }

    projectsList.innerHTML = "";
    // Default "All" option
    const allRow = document.createElement("div");
    allRow.className = "item-row" + (state.activeProjectId === null ? " active" : "");
    allRow.innerHTML = '<span class="item-title">Tous les projets</span>';
    allRow.addEventListener("click", () => {
      state.activeProjectId = null;
      activeProjectTag.classList.add("hidden");
      loadProjects();
      loadConversations();
    });
    projectsList.appendChild(allRow);

    for (const p of state.projects) {
      const row = document.createElement("div");
      row.className = "item-row" + (state.activeProjectId === p.id ? " active" : "");
      row.innerHTML = `
        <span class="item-title">${escapeHtml(p.title)}</span>
        <span class="item-meta">${p.files.length} fichier(s)</span>
      `;
      row.addEventListener("click", () => {
        state.activeProjectId = p.id;
        activeProjectTag.textContent = p.title;
        activeProjectTag.classList.remove("hidden");
        loadProjects();
        loadConversations();
      });
      projectsList.appendChild(row);
    }
  } catch (e) {
    projectsList.innerHTML = '<div class="item-meta" style="color: var(--danger-color); padding: 4px 8px;">Erreur projets</div>';
  }
}

async function loadConversations() {
  try {
    let url = "/api/conversations?limit=50";
    if (state.activeProjectId) {
      url += `&project_id=${encodeURIComponent(state.activeProjectId)}`;
    }
    const res = await fetch(url);
    const data = await res.json();
    state.conversations = data.items || [];

    if (state.conversations.length === 0) {
      conversationsList.innerHTML = '<div class="item-meta" style="padding: 12px;">Aucune conversation</div>';
      return;
    }

    conversationsList.innerHTML = "";
    for (const c of state.conversations) {
      const row = document.createElement("div");
      row.className = "item-row" + (state.activeConversationId === c.id ? " active" : "");
      row.innerHTML = `
        <span class="item-title">${escapeHtml(c.title || "Chat sans titre")}</span>
        <span class="item-meta">${c.id.substring(0, 8)}...</span>
      `;
      row.addEventListener("click", () => selectConversation(c.id));
      conversationsList.appendChild(row);
    }
  } catch (e) {
    conversationsList.innerHTML = '<div class="item-meta" style="color: var(--danger-color); padding: 12px;">Erreur conversations</div>';
  }
}

async function selectConversation(convId, forceSync = false) {
  state.activeConversationId = convId;
  const conv = state.conversations.find(c => c.id === convId);
  if (conv) {
    activeTitle.textContent = conv.title || "Chat";
  }

  // Highlight active row in sidebar
  document.querySelectorAll("#conversations-list .item-row").forEach(el => el.classList.remove("active"));
  // Re-render message branch
  messagesContainer.innerHTML = '<div style="text-align: center; color: var(--text-muted); margin-top: 50px;">Chargement de l\'historique (branche active)...</div>';

  try {
    const res = await fetch(`/api/conversations/${convId}/branch${forceSync ? "?sync=true" : ""}`);
    if (!res.ok) {
      const err = await res.json().catch(() => ({ detail: res.statusText }));
      messagesContainer.innerHTML = `<div style="color: var(--danger-color); padding: 24px;">Erreur (${res.status}): ${escapeHtml(err.detail || "Erreur chargement")}</div>`;
      return;
    }
    const branch = await res.json();
    state.activeBranchMessages = branch.messages || [];

    renderActiveBranch(state.activeBranchMessages);
  } catch (e) {
    messagesContainer.innerHTML = `<div style="color: var(--danger-color); padding: 24px;">Erreur de chargement de la branche: ${e.message}</div>`;
  }
}

function renderActiveBranch(messages) {
  if (!messages || messages.length === 0) {
    messagesContainer.innerHTML = '<div style="text-align: center; color: var(--text-muted); margin-top: 50px;">Aucun message sur cette branche.</div>';
    return;
  }

  messagesContainer.innerHTML = "";
  for (const m of messages) {
    appendMessageCard(m.role, m.content_text, m.id);
  }
  scrollToBottom();
}

function appendMessageCard(role, text, id = null) {
  const card = document.createElement("div");
  card.className = `message-card ${role}`;
  if (id) card.dataset.id = id;

  const roleLabel = role === "user" ? "Vous" : "ChatGPT";
  const avatarText = role === "user" ? "U" : "AI";
  const htmlContent = renderMarkdown(text);

  card.innerHTML = `
    <div class="avatar ${role}">${avatarText}</div>
    <div class="message-body">
      <div class="message-header">
        <span>${roleLabel}</span>
      </div>
      <div class="message-text">${htmlContent}</div>
    </div>
  `;
  messagesContainer.appendChild(card);
  return card;
}

async function handleSendMessage() {
  if (state.isStreaming) return;
  const text = chatInput.value.trim();
  if (!text) return;

  chatInput.value = "";
  state.isStreaming = true;
  btnSend.disabled = true;

  // Render user message card
  appendMessageCard("user", text);
  scrollToBottom();

  // Create assistant placeholder card
  const assistantCard = appendMessageCard("assistant", "");
  const textEl = assistantCard.querySelector(".message-text");
  textEl.innerHTML = '<span class="cursor"></span>';
  scrollToBottom();

  try {
    const res = await fetch("/api/chat/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        prompt: text,
        conversation_id: state.activeConversationId,
        project_id: state.activeProjectId,
      })
    });

    if (!res.ok) {
      const err = await res.text();
      textEl.textContent = `Erreur: ${err}`;
      return;
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let accumulatedText = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop();

      for (const line of lines) {
        const trimmed = line.trim();
        if (trimmed.startsWith("data: ")) {
          try {
            const data = JSON.parse(trimmed.slice(6));
            if (data.event === "delta") {
              accumulatedText = data.text || (accumulatedText + (data.delta || ""));
              textEl.innerHTML = renderMarkdown(accumulatedText) + '<span class="cursor"></span>';
              if (data.conversation_id && !state.activeConversationId) {
                state.activeConversationId = data.conversation_id;
              }
              scrollToBottom();
            } else if (data.event === "done") {
              accumulatedText = data.text || accumulatedText;
              textEl.innerHTML = renderMarkdown(accumulatedText);
              if (data.conversation_id) {
                state.activeConversationId = data.conversation_id;
              }
              scrollToBottom();
            } else if (data.event === "error") {
              textEl.innerHTML += `<div style="color: var(--danger-color); margin-top: 8px;">Erreur transport: ${data.error}</div>`;
            }
          } catch (e) {
            // ignore JSON parse error on incomplete chunk
          }
        }
      }
    }

    textEl.innerHTML = renderMarkdown(accumulatedText);
    await loadConversations();
  } catch (e) {
    textEl.innerHTML = `<span style="color: var(--danger-color);">Erreur réseau : ${e.message}</span>`;
  } finally {
    state.isStreaming = false;
    btnSend.disabled = false;
  }
}

function scrollToBottom() {
  messagesContainer.scrollTop = messagesContainer.scrollHeight;
}

// Configure marked with syntax highlighting and copy buttons
if (window.marked) {
  try {
    marked.use({
      breaks: true,
      gfm: true,
      renderer: {
        code(token) {
          const rawText = typeof token === "object" ? (token.text || "") : String(token || "");
          const rawLang = (typeof token === "object" ? token.lang : arguments[1]) || "";
          const lang = (rawLang || "").trim().toLowerCase();

          let highlighted = "";
          if (window.hljs && lang && window.hljs.getLanguage(lang)) {
            try {
              highlighted = window.hljs.highlight(rawText, { language: lang, ignoreIllegals: true }).value;
            } catch (e) {
              highlighted = escapeHtml(rawText);
            }
          } else if (window.hljs) {
            try {
              highlighted = window.hljs.highlightAuto(rawText).value;
            } catch (e) {
              highlighted = escapeHtml(rawText);
            }
          } else {
            highlighted = escapeHtml(rawText);
          }

          const displayLang = lang || "code";
          return `
<div class="code-block-wrapper">
  <div class="code-block-header">
    <span class="code-lang-label">${escapeHtml(displayLang)}</span>
    <button class="btn-copy-code" type="button" onclick="copyCode(this)">
      <span>📋</span> Copier
    </button>
  </div>
  <pre><code class="hljs ${escapeHtml(lang)}">${highlighted}</code></pre>
</div>
`;
        }
      }
    });
  } catch (e) {
    console.error("Failed to configure marked extensions:", e);
  }
}

function copyCode(btn) {
  const wrapper = btn.closest(".code-block-wrapper");
  if (!wrapper) return;
  const codeEl = wrapper.querySelector("code");
  if (!codeEl) return;
  const text = codeEl.innerText;
  navigator.clipboard.writeText(text).then(() => {
    const origHtml = btn.innerHTML;
    btn.innerHTML = '<span>✓</span> Copié !';
    btn.classList.add("copied");
    setTimeout(() => {
      btn.innerHTML = origHtml;
      btn.classList.remove("copied");
    }, 2000);
  }).catch(err => {
    console.error("Copy failed:", err);
  });
}
window.copyCode = copyCode;

function renderMarkdown(text) {
  if (!text) return "";
  try {
    if (window.marked && typeof window.marked.parse === "function") {
      const cleanText = text.replace(/\r\n/g, "\n");
      const html = window.marked.parse(cleanText);
      return html.replace(/<a\s+(?:[^>]*?\s+)?href="([^"]*)"([^>]*)>/gi, (match, href, rest) => {
        if (!rest.includes("target=")) {
          return `<a href="${href}" target="_blank" rel="noopener noreferrer"${rest}>`;
        }
        return match;
      });
    }
  } catch (e) {
    console.error("Markdown parsing error:", e);
  }
  return escapeHtml(text);
}

function escapeHtml(str) {
  if (!str) return "";
  return str
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}
