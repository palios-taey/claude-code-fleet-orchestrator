const PROJECTS_ENDPOINT = "/api/projects";
const PROJECT_DETAIL_ENDPOINT = (projectId) => `/api/projects/${encodeURIComponent(projectId)}`;
const PROJECT_STOP_CONDITIONS_ENDPOINT = (projectId) =>
  `/api/projects/${encodeURIComponent(projectId)}/user-stop-conditions`;
const SESSION_CURRENT_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/current`;
const SESSION_NEXT_ENDPOINT = (sessionId) => `/api/sessions/${encodeURIComponent(sessionId)}/next-ready`;
const POLL_INTERVAL_MS = 5000;
const SESSIONS = [
  "conductor",
  "weaver",
  "tutor",
  "infra",
  "taeys-hands",
  "treasurer",
  "hunter",
  "taey-ed",
  "x-claude",
];

const state = {
  paused: false,
  selectedProjectId: null,
  highlightedProjectId: null,
  projects: [],
  sessionCards: new Map(),
};

const elements = {
  projectsList: document.getElementById("projects-list"),
  projectDetail: document.getElementById("project-detail"),
  sessionsStrip: document.getElementById("sessions-strip"),
  lastUpdated: document.getElementById("last-updated"),
  pauseToggle: document.getElementById("pause-toggle"),
};

function shortHash(value) {
  if (!value) {
    return "n/a";
  }
  return String(value).slice(0, 12);
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

async function fetchJson(url) {
  const response = await fetch(url, { cache: "no-store" });
  if (!response.ok) {
    throw new Error(`${response.status} ${response.statusText} for ${url}`);
  }
  return response.json();
}

function renderStatusBadge(status) {
  const safeStatus = status || "unknown";
  return `<span class="status-badge ${escapeHtml(safeStatus)}">${escapeHtml(safeStatus)}</span>`;
}

function renderProjectList() {
  if (!state.projects.length) {
    elements.projectsList.innerHTML = '<p class="empty-hint">No projects found.</p>';
    return;
  }

  elements.projectsList.innerHTML = state.projects.map((project) => {
    const activeClass = project.id === state.selectedProjectId ? "active" : "";
    const taskCounts = [
      `pending ${project.pending ?? 0}`,
      `doing ${project.in_progress ?? 0}`,
      `done ${project.completed ?? 0}`,
      `failed ${project.failed ?? 0}`,
    ];
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
          ${taskCounts.map((count) => `<span>${escapeHtml(count)}</span>`).join("")}
        </div>
      </article>
    `;
  }).join("");

  for (const card of elements.projectsList.querySelectorAll(".project-card")) {
    card.addEventListener("click", () => {
      state.selectedProjectId = card.dataset.projectId;
      state.highlightedProjectId = card.dataset.projectId;
      renderProjectList();
      loadSelectedProject();
      renderSessionCards();
    });
  }
}

function renderSessionCards() {
  elements.sessionsStrip.innerHTML = "";

  for (const sessionId of SESSIONS) {
    const session = state.sessionCards.get(sessionId) || {};
    const current = session.current?.current;
    const nextReady = session.next?.next;
    const projectHint = current?.project_id || nextReady?.project_id || null;
    const activeClass = projectHint && projectHint === state.highlightedProjectId ? "active" : "";

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

    if (projectHint) {
      card.addEventListener("click", () => {
        state.highlightedProjectId = projectHint;
        state.selectedProjectId = projectHint;
        renderProjectList();
        renderSessionCards();
        loadSelectedProject();
      });
    }

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

function renderProjectDetail(projectSummary, stopConditions) {
  const project = projectSummary.project || {};
  const phases = projectSummary.phases || [];
  const conditions = stopConditions.conditions || [];

  elements.projectDetail.classList.remove("empty-state");
  elements.projectDetail.innerHTML = `
    <section class="project-header">
      <div class="status-row">
        <div>
          <p class="eyebrow">project ${escapeHtml(project.id || "")}</p>
          <h2>${escapeHtml(project.name || project.id || "Project")}</h2>
        </div>
        ${renderStatusBadge(project.status || "active")}
      </div>
      <p>${escapeHtml(project.description || "(no description)")}</p>
      <div class="project-meta">
        <span>source: ${escapeHtml(project.source_path || "n/a")}</span>
        <span>sha: ${escapeHtml(shortHash(project.source_sha256))}</span>
      </div>
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

async function loadProjects() {
  try {
    const data = await fetchJson(PROJECTS_ENDPOINT);
    state.projects = data.projects || [];
    if (!state.selectedProjectId && state.projects.length) {
      state.selectedProjectId = state.projects[0].id;
      state.highlightedProjectId = state.projects[0].id;
    }
    renderProjectList();
    if (state.selectedProjectId) {
      await loadSelectedProject();
    }
  } catch (error) {
    elements.projectsList.innerHTML = `<p class="empty-hint">Failed to load projects: ${escapeHtml(error.message)}</p>`;
  }
}

async function loadSelectedProject() {
  if (!state.selectedProjectId) {
    return;
  }

  try {
    const [summary, stopConditions] = await Promise.all([
      fetchJson(PROJECT_DETAIL_ENDPOINT(state.selectedProjectId)),
      fetchJson(PROJECT_STOP_CONDITIONS_ENDPOINT(state.selectedProjectId)),
    ]);
    renderProjectDetail(summary, stopConditions);
  } catch (error) {
    renderProjectError(error);
  }
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

  renderSessionCards();
}

async function refresh() {
  if (state.paused) {
    return;
  }

  await Promise.all([loadProjects(), loadSessions()]);
  elements.lastUpdated.textContent = new Date().toLocaleTimeString();
}

elements.pauseToggle.addEventListener("change", (event) => {
  state.paused = event.target.checked;
});

refresh();
window.setInterval(refresh, POLL_INTERVAL_MS);
