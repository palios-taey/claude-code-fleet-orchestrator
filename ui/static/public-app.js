const PROJECT_DETAIL_ENDPOINT = (projectId) => `/api/projects/${encodeURIComponent(projectId)}`;
const SESSION_CURRENT_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/current`;
const SESSION_NEXT_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/next-ready`;
const SESSION_PROJECTS_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/projects`;
const POLL_INTERVAL_MS = 5000;
const SESSIONS = Array.isArray(window.ORCH_PUBLIC_SESSIONS) ? window.ORCH_PUBLIC_SESSIONS : [];

const state = {
  paused: false,
  selectedSessionId: SESSIONS[0] || null,
  selectedProjectIdBySession: new Map(),
  sessionCards: new Map(),
  sessionProjects: new Map(),
};

const elements = {
  projectsList: document.getElementById("projects-list"),
  projectDetail: document.getElementById("project-detail"),
  sessionsStrip: document.getElementById("sessions-strip"),
  lastUpdated: document.getElementById("last-updated"),
  pauseToggle: document.getElementById("pause-toggle"),
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

function selectedSessionProjects() {
  return state.sessionProjects.get(state.selectedSessionId) || [];
}

function selectedProjectId() {
  return state.selectedProjectIdBySession.get(state.selectedSessionId) || null;
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
    elements.projectsList.innerHTML = '<p class="empty-hint">(no public projects for this session)</p>';
    return;
  }

  elements.projectsList.innerHTML = projects.map((project) => {
    const activeClass = project.id === currentSelection ? "active" : "";
    return `
      <article class="project-card ${activeClass}" data-project-id="${escapeHtml(project.id)}">
        <div class="status-row">
          <h3>${escapeHtml(project.name || project.id)}</h3>
          ${renderStatusBadge(project.status || "active")}
        </div>
        <div class="project-stats">
          <span><strong>${escapeHtml(project.id)}</strong></span>
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
      state.selectedProjectIdBySession.set(state.selectedSessionId, card.dataset.projectId);
      renderProjectList();
      loadSelectedProject();
    });
  }
}

function renderSessionCards() {
  elements.sessionsStrip.innerHTML = "";
  for (const sessionId of SESSIONS) {
    const session = state.sessionCards.get(sessionId) || {};
    const current = session.current?.current;
    const nextReady = session.next?.next;
    const activeClass = sessionId === state.selectedSessionId ? "active" : "";

    const card = document.createElement("article");
    card.className = `session-card ${activeClass}`.trim();
    card.innerHTML = `
      <h3 class="session-name">${escapeHtml(sessionId)}</h3>
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
      loadSelectedProject();
    });
    elements.sessionsStrip.appendChild(card);
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
        ${tasksMarkup}
      </article>
    `;
  }).join("");
}

function renderSessionSummary() {
  const session = state.sessionCards.get(state.selectedSessionId) || {};
  const current = session.current?.current;
  const nextReady = session.next?.next;

  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `
    <section class="project-header">
      <div class="status-row">
        <div>
          <p class="eyebrow">session ${escapeHtml(state.selectedSessionId || "n/a")}</p>
          <h2>${escapeHtml(state.selectedSessionId || "public")}</h2>
        </div>
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

function renderProjectDetail(projectSummary) {
  const project = projectSummary.project || {};
  const phases = projectSummary.phases || [];

  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `
    <section class="project-header">
      <div class="status-row">
        <div>
          <p class="eyebrow">session ${escapeHtml(state.selectedSessionId || "n/a")}</p>
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

    <section class="project-phases">
      ${renderPhaseCards(phases)}
    </section>
  `;
}

function renderProjectError(error) {
  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `<p class="empty-hint">Failed to load project: ${escapeHtml(error.message)}</p>`;
}

async function loadSelectedProject() {
  ensureSelectedProject();
  const projectId = selectedProjectId();
  if (!projectId) {
    renderSessionSummary();
    return;
  }

  try {
    const summary = await fetchJson(PROJECT_DETAIL_ENDPOINT(projectId));
    renderProjectDetail(summary);
  } catch (error) {
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

async function refresh() {
  if (state.paused) {
    return;
  }
  if (!SESSIONS.length) {
    elements.sessionsStrip.innerHTML = '<p class="empty-hint">(no public sessions configured)</p>';
    elements.projectsList.innerHTML = '<p class="empty-hint">(no public projects configured)</p>';
    elements.projectDetail.innerHTML = '<p class="empty-hint">No public sessions available.</p>';
    elements.lastUpdated.textContent = new Date().toLocaleTimeString();
    return;
  }

  await Promise.all([loadSessions(), loadSessionProjects()]);
  ensureSelectedProject();
  renderSessionCards();
  renderProjectList();
  await loadSelectedProject();
  elements.lastUpdated.textContent = new Date().toLocaleTimeString();
}

elements.pauseToggle.addEventListener("change", (event) => {
  state.paused = event.target.checked;
});

refresh();
window.setInterval(refresh, POLL_INTERVAL_MS);
