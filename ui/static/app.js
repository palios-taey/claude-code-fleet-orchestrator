const PROJECT_DETAIL_ENDPOINT = (projectId) => `/api/projects/${encodeURIComponent(projectId)}`;
const PROJECT_STOP_CONDITIONS_ENDPOINT = (projectId) =>
  `/api/projects/${encodeURIComponent(projectId)}/user-stop-conditions`;
const SESSION_CURRENT_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/current`;
const SESSION_NEXT_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/next-ready`;
const SESSION_PROJECTS_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/projects`;
const SESSION_NOTIFY_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/notify`;
const CHAT_ENDPOINT = (lineage) => `/api/chat/${encodeURIComponent(lineage)}`;
const CHAT_PROMOTE_ENDPOINT = (lineage) => `/api/chat/${encodeURIComponent(lineage)}/promote`;
const UI_QUESTION_ANSWER_ENDPOINT = (questionId) => `/api/ui/questions/${encodeURIComponent(questionId)}/answer`;
const POLL_INTERVAL_MS = 5000;
// Populated at runtime from GET /api/sessions (data-derived; no hardcoded operator fleet).
let SESSIONS = [];

const state = {
  paused: false,
  selectedSessionId: null,
  selectedProjectIdBySession: new Map(),
  sessionCards: new Map(),
  sessionProjects: new Map(),
  chatByLineage: new Map(),
  refDrilldowns: new Map(),
  chatStatusTimeoutId: null,
};

const elements = {
  projectsList: document.getElementById("projects-list"),
  projectDetail: document.getElementById("project-detail"),
  sessionsStrip: document.getElementById("sessions-strip"),
  lastUpdated: document.getElementById("last-updated"),
  pauseToggle: document.getElementById("pause-toggle"),
  notifyType: document.getElementById("notify-type"),
  chatBar: document.getElementById("chat-bar"),
  chatTarget: document.getElementById("chat-target"),
  chatExpandToggle: document.getElementById("chat-expand-toggle"),
  chatHistoryWrap: document.getElementById("chat-history-wrap"),
  chatNeedsYou: document.getElementById("chat-needs-you"),
  chatOpenQuestions: document.getElementById("chat-open-questions"),
  chatHistory: document.getElementById("chat-history"),
  chatForm: document.getElementById("chat-form"),
  chatInput: document.getElementById("chat-input"),
  chatSubmit: document.getElementById("chat-submit"),
  chatStatus: document.getElementById("chat-status"),
  refDialog: document.getElementById("ref-dialog"),
  refDialogTitle: document.getElementById("ref-dialog-title"),
  refDialogMeta: document.getElementById("ref-dialog-meta"),
  refDialogContent: document.getElementById("ref-dialog-content"),
  refDialogClose: document.getElementById("ref-dialog-close"),
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

function shortHash(value) {
  if (!value) {
    return "n/a";
  }
  return String(value).slice(0, 12);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const rawText = await response.text();
  let data = null;
  if (rawText) {
    try {
      data = JSON.parse(rawText);
    } catch (error) {
      throw new Error(`${response.status} ${response.statusText} for ${url}`);
    }
  }
  if (!response.ok) {
    const detail = data?.detail || data?.error || `${response.status} ${response.statusText}`;
    throw new Error(String(detail));
  }
  return data;
}

function renderStatusBadge(status) {
  const safeStatus = status || "unknown";
  return `<span class="status-badge ${escapeHtml(safeStatus)}">${escapeHtml(safeStatus)}</span>`;
}

function renderActivityBadge(activity) {
  const stateName = activity?.state === "active" ? "active" : "idle";
  return `<span class="status-badge ${stateName}">${stateName}</span>`;
}

function basename(path) {
  const parts = String(path || "").split(/[\\/]/);
  return parts[parts.length - 1] || "ref";
}

function registerRefDrilldown(payload) {
  const id = `ref-${state.refDrilldowns.size}`;
  state.refDrilldowns.set(id, payload);
  return id;
}

function renderRefSectionButton(ref, section) {
  const start = section.l_start ?? ref.l_start ?? "?";
  const end = section.l_end ?? ref.l_end ?? "?";
  const pointer = `${basename(ref.path)}:L${start}-L${end}`;
  const id = registerRefDrilldown({
    title: pointer,
    path: ref.path || "",
    start,
    end,
    provenanceHash: ref.provenance_hash || "",
    content: section.content || "",
    warning: section.warning || ref.warning || "",
  });
  return `<button type="button" class="ref-pointer" data-ref-id="${escapeHtml(id)}">${escapeHtml(pointer)}</button>`;
}

function renderRefContext(refContext) {
  const refs = refContext?.refs || [];
  if (!refs.length) {
    return '<span class="muted">(no refs)</span>';
  }
  return refs.map((ref) => {
    const sections = ref.sections?.length ? ref.sections : [{ l_start: ref.l_start, l_end: ref.l_end, content: ref.content, warning: ref.warning }];
    const label = ref.label ? `<strong>${escapeHtml(ref.label)}</strong> ` : "";
    return `<div class="ref-row">${label}${sections.map((section) => renderRefSectionButton(ref, section)).join(" ")}</div>`;
  }).join("");
}

function renderNamedRefTier(name, refContext) {
  return `
    <section class="ref-tier" data-ref-level="${escapeHtml(name)}">
      <h3>${escapeHtml(name)} refs</h3>
      ${renderRefContext(refContext)}
    </section>
  `;
}

function renderRefDialog(refId) {
  const ref = state.refDrilldowns.get(refId);
  if (!ref || !elements.refDialog) {
    return;
  }
  elements.refDialogTitle.textContent = ref.title;
  elements.refDialogMeta.textContent = [
    ref.path,
    ref.provenanceHash ? `provenance ${shortHash(ref.provenanceHash)}` : "",
  ].filter(Boolean).join(" | ");
  elements.refDialogContent.textContent = ref.warning || ref.content || "(empty)";
  if (typeof elements.refDialog.showModal === "function") {
    elements.refDialog.showModal();
  } else {
    window.alert(`${ref.title}\n${elements.refDialogMeta.textContent}\n\n${elements.refDialogContent.textContent}`);
  }
}

function selectedSessionProjects() {
  return state.sessionProjects.get(state.selectedSessionId) || [];
}

function selectedProjectId() {
  return state.selectedProjectIdBySession.get(state.selectedSessionId) || null;
}

function selectedLineage() {
  return state.selectedSessionId;
}

function ensureSelectedProject() {
  const projects = selectedSessionProjects();
  const currentSelection = selectedProjectId();
  if (!projects.length) {
    state.selectedProjectIdBySession.delete(state.selectedSessionId);
    return;
  }
  if (currentSelection && projects.some((project) => project.id === currentSelection)) {
    return;
  }
  state.selectedProjectIdBySession.set(state.selectedSessionId, projects[0].id);
}

function renderProjectList() {
  const projects = selectedSessionProjects();
  const currentSelection = selectedProjectId();

  if (!projects.length) {
    elements.projectsList.innerHTML = '<p class="empty-hint">(no projects with tasks owned by this session)</p>';
    return;
  }

  elements.projectsList.innerHTML = projects.map((project) => {
    const activeClass = project.id === currentSelection ? "active" : "";
    return `
      <article class="project-card ${activeClass}" data-project-id="${escapeHtml(project.id)}">
        <div class="status-row">
          <div>
            <h3>${escapeHtml(project.name || project.id)}</h3>
            <p class="project-slug">${escapeHtml(project.id || "")}</p>
          </div>
          ${renderStatusBadge(project.status || "active")}
        </div>
        <div class="project-stats">
          <span>${project.phase_count ?? 0} phases</span>
          <span>${project.task_total ?? 0} tasks</span>
        </div>
        <div class="project-stats">
          <span>pending ${project.pending ?? 0}</span>
          <span>doing ${project.in_progress ?? 0}</span>
          <span>done ${project.completed ?? 0}</span>
          <span>failed ${project.failed ?? 0}</span>
        </div>
      </article>
    `;
  }).join("");

  for (const card of elements.projectsList.querySelectorAll(".project-card")) {
    card.addEventListener("click", () => {
      const projectId = card.dataset.projectId;
      if (projectId === selectedProjectId()) {
        return;
      }
      state.selectedProjectIdBySession.set(state.selectedSessionId, projectId);
      renderProjectList();
      renderProjectLoading(projectId);
      loadSelectedProject(projectId);
    });
  }
}

function renderSessionCards() {
  elements.sessionsStrip.innerHTML = "";
  for (const sessionId of SESSIONS) {
    const session = state.sessionCards.get(sessionId) || {};
    const current = session.current?.current;
    const activity = session.current?.activity;
    const nextReady = session.next?.next;
    const activeClass = sessionId === state.selectedSessionId ? "active" : "";
    const chat = state.chatByLineage.get(sessionId) || {};
    const needsYou = chat.needs_you || (chat.open_questions || []).length;

    const card = document.createElement("article");
    card.className = `session-card ${activeClass}`.trim();
    card.innerHTML = `
      <div class="status-row">
        <h3 class="session-name">${escapeHtml(sessionId)}</h3>
        ${renderActivityBadge(activity)}
        ${needsYou ? '<span class="status-badge blocked">needs you</span>' : ""}
      </div>
      <div class="session-line">
        <strong>Current:</strong>
        ${current ? escapeHtml(current.top_task_id) : '<span class="muted">idle</span>'}
      </div>
      <div class="session-line">
        <strong>Next:</strong>
        ${nextReady ? escapeHtml(nextReady.task_id) : '<span class="muted">none</span>'}
      </div>
    `;
    card.addEventListener("click", () => {
      state.selectedSessionId = sessionId;
      ensureSelectedProject();
      renderSessionCards();
      renderProjectList();
      renderSessionSummary();
      syncChatTarget();
      renderChatPanel();
      loadSelectedProject();
    });
    elements.sessionsStrip.appendChild(card);
  }
}

function renderOpenQuestions(openQuestions) {
  if (!openQuestions.length) {
    return '<p class="muted">(no open questions)</p>';
  }
  return `
    <section class="stop-conditions">
      <h3>Open questions</h3>
      <ul>${openQuestions.map((item) => {
        const questionId = item.question_id || item.id || "";
        return `
          <li>
            <p>${escapeHtml(item.reason || item.text || "")}</p>
            ${questionId ? `
              <div class="question-answer" data-question-id="${escapeHtml(questionId)}">
                <textarea rows="2" placeholder="Verdict"></textarea>
                <button type="button" data-answer-question="${escapeHtml(questionId)}">Record verdict</button>
              </div>
            ` : ""}
          </li>
        `;
      }).join("")}</ul>
    </section>
  `;
}

function renderChatMessages(messages) {
  if (!messages.length) {
    return '<p class="empty-hint">(no chat messages yet)</p>';
  }
  return messages.map((message) => `
    <article class="summary-card" data-message-id="${escapeHtml(message.id || "")}">
      <div class="status-row">
        <strong>${escapeHtml(message.sender || message.role || "unknown")}</strong>
        <span class="muted">${escapeHtml(message.ts || "")}</span>
      </div>
      <p>${escapeHtml(message.text || "")}</p>
      ${message.role === "user" ? `<button type="button" data-promote-reply="${escapeHtml(message.id || "")}">Promote to MEMORY</button>` : ""}
    </article>
  `).join("");
}

function renderChatPanel() {
  const chat = state.chatByLineage.get(selectedLineage()) || {};
  const needsYou = chat.needs_you || (chat.open_questions || []).length;
  elements.chatNeedsYou.hidden = !needsYou;
  elements.chatOpenQuestions.innerHTML = renderOpenQuestions(chat.open_questions || []);
  elements.chatHistory.innerHTML = renderChatMessages(chat.messages || []);
  // newest at the bottom — keep the latest exchange in view when expanded
  if (!elements.chatHistoryWrap.hidden) {
    elements.chatHistoryWrap.scrollTop = elements.chatHistoryWrap.scrollHeight;
  }
}

function renderPhaseCards(phases) {
  if (!phases.length) {
    return '<p class="empty-hint">No phases found for this project.</p>';
  }

  return phases.map((item) => {
    const phase = item.phase || {};
    const tasks = item.tasks || [];
    const counts = item.task_counts || {};
    const tasksMarkup = tasks.length ? `
      <table class="tasks-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>Status</th>
            <th>Owner</th>
            <th>Priority</th>
            <th>Blocked On</th>
            <th>Refs</th>
          </tr>
        </thead>
        <tbody>
          ${tasks.map((task) => `
            <tr>
              <td data-label="ID">
                <span class="task-id">${escapeHtml(task.id)}</span>
                <span class="task-description">${escapeHtml(task.description || "")}</span>
              </td>
              <td data-label="Status">${renderStatusBadge(task.status || "pending")}</td>
              <td data-label="Owner">${task.owner ? escapeHtml(task.owner) : '<span class="muted">(unowned)</span>'}</td>
              <td data-label="Priority"><span class="priority-pill">${escapeHtml(task.priority ?? "?")}</span></td>
              <td data-label="Blocked On">
                ${task.blocked_on ? `<span class="blocked-pill">${escapeHtml(task.blocked_on)}</span>` : '<span class="muted">(none)</span>'}
              </td>
              <td data-label="Refs">${renderRefContext(task.ref_context)}</td>
            </tr>
          `).join("")}
        </tbody>
      </table>
    ` : '<p class="empty-hint">No tasks in this phase.</p>';

    return `
      <article class="phase-card">
        <div class="phase-header">
          <div class="phase-title">
            <h3>${escapeHtml(phase.name || phase.id || "Phase")}</h3>
            <div class="project-meta">
              <span>${escapeHtml(phase.id || "")}</span>
              <span>order ${escapeHtml(phase.order ?? 0)}</span>
            </div>
          </div>
          <div class="mini-bars">
            <span class="mini-bar pending">P ${escapeHtml(counts.pending ?? 0)}</span>
            <span class="mini-bar in_progress">D ${escapeHtml(counts.in_progress ?? 0)}</span>
            <span class="mini-bar completed">C ${escapeHtml(counts.completed ?? 0)}</span>
            <span class="mini-bar failed">F ${escapeHtml(counts.failed ?? 0)}</span>
          </div>
        </div>
        ${renderNamedRefTier("phase", phase.ref_context)}
        ${tasksMarkup}
      </article>
    `;
  }).join("");
}

function renderSessionSummary() {
  state.refDrilldowns.clear();
  const session = state.sessionCards.get(state.selectedSessionId) || {};
  const current = session.current?.current;
  const activity = session.current?.activity;
  const nextReady = session.next?.next;

  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `
    <section class="project-header">
      <div class="status-row">
        <div>
          <p class="eyebrow">session ${escapeHtml(state.selectedSessionId)}</p>
          <h2>${escapeHtml(state.selectedSessionId)}</h2>
        </div>
        ${renderActivityBadge(activity)}
      </div>
      <div class="summary-grid">
        <div class="summary-card">
          <h3>Current</h3>
          <p>${current ? escapeHtml(current.top_task_id) : '<span class="muted">idle</span>'}</p>
          <p class="muted">${current ? escapeHtml(current.top_task_desc || "") : "No in-progress task."}</p>
        </div>
        <div class="summary-card">
          <h3>Next ready</h3>
          <p>${nextReady ? escapeHtml(nextReady.task_id) : '<span class="muted">none</span>'}</p>
          <p class="muted">${nextReady ? escapeHtml(nextReady.description || "") : "No ready task exposed by the API."}</p>
        </div>
      </div>
    </section>
  `;
}

function renderProjectDetail(projectSummary, stopConditions) {
  state.refDrilldowns.clear();
  const project = projectSummary.project || {};
  const phases = projectSummary.phases || [];
  const tiers = projectSummary.ref_tiers || {};
  const conditions = stopConditions.conditions || [];

  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `
    <section class="project-header">
      <div class="status-row">
        <div>
          <p class="eyebrow">session ${escapeHtml(state.selectedSessionId)}</p>
          <h2>${escapeHtml(project.name || project.id || "Project")}</h2>
        </div>
        ${renderStatusBadge(project.status || "active")}
      </div>
      <p>${escapeHtml(project.description || "(no description)")}</p>
      <div class="project-meta">
        <span>project: ${escapeHtml(project.id || "")}</span>
        <span>source: ${escapeHtml(project.source_path || "n/a")}</span>
        <span>sha: ${escapeHtml(shortHash(project.source_sha256))}</span>
      </div>
    </section>

    <section class="refs-panel">
      ${renderNamedRefTier("overall", tiers.overall?.ref_context)}
      ${renderNamedRefTier("supervisor", tiers.supervisor?.ref_context)}
      ${renderNamedRefTier("project", project.ref_context)}
    </section>

    <section class="stop-conditions">
      <h3>User-stop-conditions</h3>
      ${conditions.length ? `<ul>${conditions.map((condition) => `<li>${escapeHtml(condition)}</li>`).join("")}</ul>` : '<p class="muted">(none)</p>'}
    </section>

    <section class="project-phases">
      ${renderPhaseCards(phases)}
    </section>
  `;
}

function renderProjectError(error) {
  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `<p class="empty-hint">Failed to load project: ${escapeHtml(error.message)}</p>`;
}

function renderProjectLoading(projectId) {
  state.refDrilldowns.clear();
  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `<p class="empty-hint">Loading ${escapeHtml(projectId)}...</p>`;
}

function syncChatTarget() {
  const target = selectedLineage();
  elements.chatTarget.textContent = target || "selected session";
  elements.chatInput.placeholder = target ? `Message to ${target}...` : "Message...";
}

function setChatStatus(message, kind, durationMs) {
  elements.chatStatus.textContent = message;
  elements.chatStatus.className = `notify-status ${kind}`.trim();
  if (state.chatStatusTimeoutId) {
    window.clearTimeout(state.chatStatusTimeoutId);
  }
  if (durationMs > 0) {
    state.chatStatusTimeoutId = window.setTimeout(() => {
      elements.chatStatus.textContent = "";
      elements.chatStatus.className = "notify-status";
    }, durationMs);
  }
}

function updateChatButtonState() {
  elements.chatSubmit.disabled = !elements.chatInput.value.trim();
}

async function loadSelectedProject(expectedProjectId = selectedProjectId()) {
  ensureSelectedProject();
  const projectId = expectedProjectId || selectedProjectId();
  if (!projectId) {
    renderSessionSummary();
    return;
  }

  try {
    const [summary, stopConditions] = await Promise.all([
      fetchJson(PROJECT_DETAIL_ENDPOINT(projectId)),
      fetchJson(PROJECT_STOP_CONDITIONS_ENDPOINT(projectId)),
    ]);
    if (projectId !== selectedProjectId()) {
      return;
    }
    renderProjectDetail(summary, stopConditions);
  } catch (error) {
    if (projectId !== selectedProjectId()) {
      return;
    }
    renderProjectError(error);
  }
}

async function loadSessionProjects() {
  await Promise.all(SESSIONS.map(async (sessionId) => {
    try {
      const result = await fetchJson(SESSION_PROJECTS_ENDPOINT(sessionId));
      state.sessionProjects.set(sessionId, result.projects || []);
    } catch (error) {
      state.sessionProjects.set(sessionId, []);
    }
  }));
}

async function loadSessions() {
  await Promise.all(SESSIONS.map(async (sessionId) => {
    try {
      const [current, next] = await Promise.all([
        fetchJson(SESSION_CURRENT_ENDPOINT(sessionId)),
        fetchJson(SESSION_NEXT_ENDPOINT(sessionId)),
      ]);
      state.sessionCards.set(sessionId, { current, next });
    } catch (error) {
      state.sessionCards.set(sessionId, {
        current: { current: null },
        next: { next: null },
        error: error.message,
      });
    }
  }));
}

async function loadChat() {
  await Promise.all(SESSIONS.map(async (sessionId) => {
    try {
      const result = await fetchJson(CHAT_ENDPOINT(sessionId));
      state.chatByLineage.set(sessionId, result);
    } catch (error) {
      state.chatByLineage.set(sessionId, {
        lineage: sessionId,
        messages: [],
        open_questions: [],
        needs_you: null,
        error: error.message,
      });
    }
  }));
}

async function loadSessionList() {
  try {
    const data = await fetchJson("/api/sessions");
    SESSIONS = Array.isArray(data.sessions) ? data.sessions : [];
  } catch (error) {
    if (!Array.isArray(SESSIONS)) {
      SESSIONS = [];
    }
  }
  if (!state.selectedSessionId || !SESSIONS.includes(state.selectedSessionId)) {
    state.selectedSessionId = SESSIONS[0] || null;
  }
}

let _refreshing = false;
async function refresh() {
  // In-flight guard: the 5s setInterval must not overlap — a slow
  // refresh resolving after a newer one would clobber DOM/state. Skip if one is still running.
  if (state.paused || _refreshing) {
    return;
  }
  _refreshing = true;
  try {
    await loadSessionList();
    await Promise.all([loadSessions(), loadSessionProjects(), loadChat()]);
    ensureSelectedProject();
    renderSessionCards();
    renderProjectList();
    renderChatPanel();
    await loadSelectedProject();
    elements.lastUpdated.textContent = new Date().toLocaleTimeString();
  } finally {
    _refreshing = false;
  }
}

elements.pauseToggle.addEventListener("change", (event) => {
  state.paused = event.target.checked;
});

elements.chatInput.addEventListener("input", updateChatButtonState);

function setChatExpanded(open) {
  elements.chatHistoryWrap.hidden = !open;
  elements.chatExpandToggle.setAttribute("aria-expanded", String(open));
  elements.chatBar.classList.toggle("expanded", open);
  if (open) {
    elements.chatHistoryWrap.scrollTop = elements.chatHistoryWrap.scrollHeight;
  }
}

elements.chatExpandToggle.addEventListener("click", () => {
  setChatExpanded(elements.chatHistoryWrap.hidden);
});

elements.projectDetail.addEventListener("click", (event) => {
  const target = event.target.closest("[data-ref-id]");
  if (!target) {
    return;
  }
  renderRefDialog(target.dataset.refId);
});

if (elements.refDialogClose) {
  elements.refDialogClose.addEventListener("click", () => {
    elements.refDialog.close();
  });
}

elements.chatHistory.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-promote-reply]");
  if (!target) {
    return;
  }
  const chat = state.chatByLineage.get(selectedLineage()) || {};
  const message = (chat.messages || []).find((item) => item.id === target.dataset.promoteReply);
  if (!message) {
    return;
  }
  setChatStatus("Promoting...", "pending", 0);
  try {
    await fetchJson(CHAT_PROMOTE_ENDPOINT(selectedLineage()), {
      method: "POST",
      body: JSON.stringify({
        sender: message.sender || "jesse",
        reply: message.text || "",
      }),
    });
    setChatStatus("Promoted to MEMORY", "success", 5000);
  } catch (error) {
    setChatStatus(error.message, "error", 8000);
  }
});

elements.chatOpenQuestions.addEventListener("click", async (event) => {
  const target = event.target.closest("[data-answer-question]");
  if (!target) {
    return;
  }
  const questionId = target.dataset.answerQuestion;
  const container = target.closest("[data-question-id]");
  const answer = container?.querySelector("textarea")?.value.trim() || "";
  if (!questionId || !answer) {
    setChatStatus("Verdict required", "error", 5000);
    return;
  }
  target.disabled = true;
  setChatStatus("Recording verdict...", "pending", 0);
  try {
    await fetchJson(UI_QUESTION_ANSWER_ENDPOINT(questionId), {
      method: "POST",
      body: JSON.stringify({ answer, answered_by: "jesse" }),
    });
    await Promise.all([loadChat(), loadSessions(), loadSessionProjects()]);
    ensureSelectedProject();
    renderSessionCards();
    renderProjectList();
    renderChatPanel();
    await loadSelectedProject();
    setChatStatus("Verdict recorded", "success", 5000);
  } catch (error) {
    target.disabled = false;
    setChatStatus(error.message, "error", 8000);
  }
});

elements.chatForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const text = elements.chatInput.value.trim();
  if (!text) {
    return;
  }
  const target = selectedLineage();

  elements.chatSubmit.disabled = true;
  // Optimistic echo: show the message in the thread immediately (like a normal
  // AI chat), expand the panel if collapsed, then reconcile against the server.
  const chat = state.chatByLineage.get(target) || { lineage: target, messages: [], open_questions: [] };
  chat.messages = [...(chat.messages || []), { id: `local-${Date.now()}`, sender: "jesse", role: "user", text, ts: "sending…" }];
  state.chatByLineage.set(target, chat);
  elements.chatInput.value = "";
  updateChatButtonState();
  setChatExpanded(true);
  renderChatPanel();
  setChatStatus("Sending…", "pending", 0);
  try {
    // 1) persist to the two-way history stream, 2) deliver to the session inbox.
    // The chat endpoint only stores; delivery to the live session is the notify path.
    await fetchJson(CHAT_ENDPOINT(target), {
      method: "POST",
      body: JSON.stringify({ sender: "jesse", role: "user", text }),
    });
    await fetchJson(SESSION_NOTIFY_ENDPOINT(target), {
      method: "POST",
      body: JSON.stringify({ type: elements.notifyType.value, message: text }),
    });
    await loadChat();
    renderChatPanel();
    renderSessionCards();
    setChatStatus("Sent ✓", "success", 5000);
  } catch (error) {
    // restore the message so it is not lost, drop the optimistic echo
    elements.chatInput.value = text;
    updateChatButtonState();
    await loadChat();
    renderChatPanel();
    setChatStatus(error.message, "error", 8000);
  }
});

(async () => {
  await loadSessionList();
  syncChatTarget();
  setChatExpanded(true);
  updateChatButtonState();
  await refresh();
  window.setInterval(refresh, POLL_INTERVAL_MS);
})();
