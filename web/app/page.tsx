"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api, apiUrl } from "@/lib/api";

type Tab = "overview" | "agents" | "tasks" | "brain" | "content" | "providers" | "settings";
type AgentDefinition = { key: string; name: string; description: string; capabilities: string[]; maturity: string; dispatch_ready: boolean };
type CompanyAgent = { agent_key: string; status: string; definition: AgentDefinition };
type Task = { id: string; agent_key: string; capability: string; title: string; status: string; created_at: string; attempt_count: number; max_attempts: number; error_message?: string | null };
type Provider = { provider_id: string; display_name: string; description: string; capabilities: string[] };
type ProviderHealth = { provider_id: string; calls_30d: number; errors_30d: number; success_rate: number | null; average_latency_ms: number | null; last_call_at: string };
type SceneProvider = { provider_id: string; display_name: string; requires_network: boolean; supports_lipsync: boolean; supports_reference_image: boolean; supports_cancellation: boolean; daily_quota: number | null };
type Budget = { provider_id: string; monthly_limit_usd: string; per_operation_limit_usd: string; enabled: boolean };
type EventItem = { id: string; sequence: number; event_type: string; payload: Record<string, unknown>; created_at: string };
type Brand = { id: string; name: string; display_name?: string; industry?: string; status: string };
type ContentProject = { id: string; brand_id: string; topic: string; status: string; video_mode: "avatar" | "scene"; error_message?: string | null; video_workflow_execution_id?: string | null; final_artifact_id?: string | null };
type Platform = { id: string; name: string; base_url?: string; platform_type?: string };

const nav: { id: Tab; label: string; icon: string }[] = [
  { id: "overview", label: "نمای کلی", icon: "◫" },
  { id: "agents", label: "مرکز Agentها", icon: "✳" },
  { id: "tasks", label: "مرکز وظایف", icon: "▤" },
  { id: "brain", label: "Company Brain", icon: "⌘" },
  { id: "content", label: "استودیو محتوا", icon: "▧" },
  { id: "providers", label: "Provider Center", icon: "⇄" },
  { id: "settings", label: "تنظیمات", icon: "⚙" },
];

function statusLabel(status: string) {
  const labels: Record<string, string> = {
    completed: "تکمیل‌شده", running: "در حال اجرا", queued: "در صف",
    pending: "در انتظار", awaiting_approval: "نیازمند تأیید",
    failed: "ناموفق", cancelled: "لغوشده", active: "فعال",
    inactive: "غیرفعال", settled: "تسویه‌شده", reserved: "رزروشده",
    unknown: "هزینه نامشخص",
  };
  return labels[status] || status;
}

function Status({ value }: { value: string }) {
  return <span className={`status ${value}`}>{statusLabel(value)}</span>;
}

export default function HomePage() {
  const [tab, setTab] = useState<Tab>("overview");
  const [companyId, setCompanyId] = useState("");
  const [tasks, setTasks] = useState<Task[]>([]);
  const [catalog, setCatalog] = useState<AgentDefinition[]>([]);
  const [companyAgents, setCompanyAgents] = useState<CompanyAgent[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [providerHealth, setProviderHealth] = useState<ProviderHealth[]>([]);
  const [sceneProviders, setSceneProviders] = useState<SceneProvider[]>([]);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown>[]>([]);
  const [reconcileAmounts, setReconcileAmounts] = useState<Record<string, string>>({});
  const [brands, setBrands] = useState<Brand[]>([]);
  const [projects, setProjects] = useState<ContentProject[]>([]);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [taskAgent, setTaskAgent] = useState("monitoring");
  const [taskCapability, setTaskCapability] = useState("platform.monitor");
  const [taskTitle, setTaskTitle] = useState("رصد و تحلیل پلتفرم");
  const [taskPayload, setTaskPayload] = useState('{"platform_id": "", "connection_id": ""}');
  const [budgetProvider, setBudgetProvider] = useState("llm_openai");
  const [monthlyLimit, setMonthlyLimit] = useState("20");
  const [operationLimit, setOperationLimit] = useState("2");
  const [contentMode, setContentMode] = useState<"avatar" | "scene">("scene");
  const [contentBrandId, setContentBrandId] = useState("");
  const [contentTopic, setContentTopic] = useState("");
  const [llmConnectionId, setLlmConnectionId] = useState("");
  const [videoConnectionId, setVideoConnectionId] = useState("");
  const [sceneProviderId, setSceneProviderId] = useState("apimaster");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      api<{ items: AgentDefinition[] }>("/agents/catalog"),
      api<CompanyAgent[]>("/agents"),
      api<{ items: Task[] }>("/agent-tasks?limit=50"),
      api<Provider[]>("/providers"),
      api<ProviderHealth[]>("/providers/telemetry"),
      api<SceneProvider[]>("/providers/scene-video"),
      api<Budget[]>("/providers/budgets"),
      api<Record<string, unknown>[]>("/providers/usage?limit=50"),
      api<Brand[]>("/content/brands"),
      api<ContentProject[]>("/content/projects"),
      api<{ items: Platform[] }>("/platforms"),
    ] as const);
    const [catalogResult, agentsResult, tasksResult, providersResult, healthResult, sceneProvidersResult, budgetsResult, usageResult, brandsResult, projectsResult, platformsResult] = results;
    if (catalogResult.status === "fulfilled") setCatalog(catalogResult.value.items || []);
    if (agentsResult.status === "fulfilled") setCompanyAgents(agentsResult.value || []);
    if (tasksResult.status === "fulfilled") setTasks(tasksResult.value.items || []);
    if (providersResult.status === "fulfilled") setProviders(providersResult.value || []);
    if (healthResult.status === "fulfilled") setProviderHealth(healthResult.value || []);
    if (sceneProvidersResult.status === "fulfilled") setSceneProviders(sceneProvidersResult.value || []);
    if (budgetsResult.status === "fulfilled") setBudgets(budgetsResult.value || []);
    if (usageResult.status === "fulfilled") setUsage(usageResult.value || []);
    if (brandsResult.status === "fulfilled") setBrands(brandsResult.value || []);
    if (projectsResult.status === "fulfilled") setProjects(projectsResult.value || []);
    if (platformsResult.status === "fulfilled") setPlatforms(platformsResult.value.items || []);
    const failures = results.filter((item) => item.status === "rejected");
    if (failures.length === results.length) setError("ارتباط با API برقرار نشد. آدرس Backend و دسترسی شرکت را بررسی کنید.");
    else if (failures.length) setNotice("بخشی از اطلاعات در دسترس نبود؛ سایر بخش‌ها بارگذاری شدند.");
    setLoading(false);
  }, []);

  useEffect(() => {
    const saved = window.localStorage.getItem("vandad.companyId") || process.env.NEXT_PUBLIC_COMPANY_ID || "";
    setCompanyId(saved);
    if (saved) window.localStorage.setItem("vandad.companyId", saved);
    void load();
  }, [load]);

  const selectedDefinition = useMemo(
    () => catalog.find((agent) => agent.key === taskAgent),
    [catalog, taskAgent],
  );

  useEffect(() => {
    if (selectedDefinition?.capabilities.length) {
      setTaskCapability(selectedDefinition.capabilities[0]);
    }
  }, [selectedDefinition]);

  useEffect(() => {
    const templates: Record<string, Record<string, string | number>> = {
      "platform.monitor": { platform_id: "", connection_id: "", max_pages: 10, max_documents: 20 },
      "platform.discover": { platform_id: "", max_pages: 10 },
      "platform.change.detect": { platform_id: "", max_pages: 10 },
      "platform.knowledge.extract": { platform_id: "", connection_id: "", max_documents: 20 },
      "content.scenario.generate": { project_id: "", connection_id: "", timeout_seconds: 180 },
      "video.scene.generate": { project_id: "", provider_id: "apimaster" },
      "video.avatar.generate": { project_id: "", connection_id: "" },
    };
    const template = templates[taskCapability];
    if (template) setTaskPayload(JSON.stringify(template, null, 2));
  }, [taskCapability]);

  const run = async (action: () => Promise<unknown>, success: string) => {
    setError("");
    setNotice("");
    try {
      await action();
      setNotice(success);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "عملیات انجام نشد.");
    }
  };

  const saveCompany = () => {
    const value = companyId.trim();
    if (value) window.localStorage.setItem("vandad.companyId", value);
    else window.localStorage.removeItem("vandad.companyId");
    setNotice("شناسه شرکت ذخیره شد.");
    void load();
  };

  const createTask = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await run(async () => {
      const input_payload = JSON.parse(taskPayload) as Record<string, unknown>;
      await api("/agent-tasks", {
        method: "POST",
        body: JSON.stringify({
          agent_key: taskAgent,
          capability: taskCapability,
          title: taskTitle,
          input_payload,
          max_attempts: 3,
          priority: 50,
        }),
      });
    }, "وظیفه ثبت شد.");
  };

  const showEvents = async (taskId: string) => {
    setSelectedTask(taskId);
    try {
      const rows = await api<EventItem[]>(`/agent-tasks/${taskId}/events?limit=200`);
      setEvents(rows);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "دریافت رویدادها ناموفق بود.");
    }
  };

  useEffect(() => {
    if (!selectedTask) return;
    const controller = new AbortController();
    let lastSequence = 0;
    const headers = new Headers({ Accept: "text/event-stream" });
    if (companyId) headers.set("X-Company-ID", companyId);

    const streamEvents = async () => {
      while (!controller.signal.aborted) {
        try {
          const streamHeaders = new Headers(headers);
          if (lastSequence > 0) streamHeaders.set("Last-Event-ID", String(lastSequence));
          const response = await fetch(`${apiUrl}/agent-tasks/${selectedTask}/events/stream`, {
            headers: streamHeaders,
            signal: controller.signal,
            cache: "no-store",
            credentials: "include",
          });
          if (!response.ok || !response.body) throw new Error(`SSE request failed (${response.status})`);
          const reader = response.body.getReader();
          const decoder = new TextDecoder();
          let buffer = "";
          while (!controller.signal.aborted) {
            const chunk = await reader.read();
            if (chunk.done) break;
            buffer += decoder.decode(chunk.value, { stream: true });
            const blocks = buffer.split("\n\n");
            buffer = blocks.pop() || "";
            for (const block of blocks) {
              const dataLine = block.split("\n").find((line) => line.startsWith("data:"));
              if (!dataLine) continue;
              try {
                const payload = JSON.parse(dataLine.slice(5).trim()) as {
                  id: string; sequence: number; type: string;
                  payload: Record<string, unknown>; created_at: string;
                };
                if (!payload.sequence || payload.sequence <= lastSequence) continue;
                lastSequence = payload.sequence;
                const item: EventItem = {
                  id: payload.id,
                  sequence: payload.sequence,
                  event_type: payload.type,
                  payload: payload.payload || {},
                  created_at: payload.created_at,
                };
                setEvents((current) => [...current.filter((event) => event.sequence !== item.sequence), item]
                  .sort((a, b) => a.sequence - b.sequence).slice(-200));

              } catch {
                // Ignore malformed SSE frames; the durable event endpoint remains available.
              }
            }
          }
          const currentTask = await api<Task>(`/agent-tasks/${selectedTask}`);
          if (["completed", "failed", "cancelled"].includes(currentTask.status)) {
            void load();
            return;
          }
        } catch (cause) {
          if (controller.signal.aborted) return;
          setError(cause instanceof Error ? cause.message : "ارتباط زنده رویدادها قطع شد.");
        }
        await new Promise((resolve) => window.setTimeout(resolve, 1200));
      }
    };

    void streamEvents();
    return () => controller.abort();
  }, [selectedTask, companyId, load]);

  const createContentProject = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await run(async () => {
      if (!contentBrandId) throw new Error("ابتدا یک برند انتخاب کنید.");
      if (contentMode === "avatar") {
        await api("/content/projects", {
          method: "POST",
          body: JSON.stringify({ brand_id: contentBrandId, topic: contentTopic }),
        });
      } else {
        await api("/content/scene-projects", {
          method: "POST",
          body: JSON.stringify({
            brand_id: contentBrandId,
            topic: contentTopic,
            target_duration_seconds: 30,
            provider_id: sceneProviderId || undefined,
          }),
        });
      }
      setContentTopic("");
    }, "پروژه محتوا ایجاد شد.");
  };

  const generateProjectScript = async (project: ContentProject) => {
    await run(async () => {
      if (!llmConnectionId.trim()) throw new Error("شناسه اتصال LLM را وارد کنید.");
      const prefix = project.video_mode === "scene" ? "/content/scene-projects" : "/content/projects";
      const action = project.video_mode === "scene" ? "generate-scenario" : "generate-script";
      await api(`${prefix}/${project.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ connection_id: llmConnectionId.trim() }),
      });
    }, "درخواست تولید سناریو ثبت شد.");
  };

  const approveContentProject = async (project: ContentProject) => {
    await run(async () => {
      const prefix = project.video_mode === "scene" ? "/content/scene-projects" : "/content/projects";
      const action = project.video_mode === "scene" ? "approve-scenario" : "approve-script";
      await api(`${prefix}/${project.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({ approved: true }),
      });
    }, "سناریو تأیید شد.");
  };

  const generateProjectVideo = async (project: ContentProject) => {
    await run(async () => {
      if (project.video_mode === "avatar") {
        if (!videoConnectionId.trim()) throw new Error("شناسه اتصال HeyGen را وارد کنید.");
        await api(`/content/projects/${project.id}/generate-video`, {
          method: "POST",
          body: JSON.stringify({ connection_id: videoConnectionId.trim() }),
        });
      } else {
        await api(`/content/scene-projects/${project.id}/generate-video`, {
          method: "POST",
          body: JSON.stringify({ provider_id: sceneProviderId || undefined }),
        });
      }
    }, "Workflow تولید ویدیو به صف ارسال شد.");
  };

  const reconcileUsage = async (recordId: string) => {
    await run(async () => {
      const amount = reconcileAmounts[recordId];
      if (!amount || Number(amount) < 0) throw new Error("هزینه واقعی را وارد کنید.");
      await api(`/providers/usage/${recordId}/reconcile`, {
        method: "POST",
        body: JSON.stringify({
          actual_cost_usd: amount,
          note: "Verified against the provider billing record by the company operator.",
        }),
      });
    }, "هزینه واقعی ثبت و رزرو قبلی تسویه شد.");
  };

  const createBudget = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    await run(async () => {
      await api(`/providers/budgets/${budgetProvider}`, {
        method: "PUT",
        body: JSON.stringify({
          monthly_limit_usd: monthlyLimit,
          per_operation_limit_usd: operationLimit,
          enabled: true,
        }),
      });
    }, "سقف هزینه Provider ذخیره شد.");
  };

  const activeCount = companyAgents.filter((agent) => agent.status === "active").length;
  const runningCount = tasks.filter((task) => task.status === "running").length;
  const awaitingCount = tasks.filter((task) => task.status === "awaiting_approval").length;
  const completedCount = tasks.filter((task) => task.status === "completed").length;

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">V</div>
          <div><strong>Vandad Agent</strong><small>Operations Intelligence</small></div>
        </div>
        <nav className="nav">
          {nav.map((item) => (
            <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>
              <span className="nav-icon">{item.icon}</span>{item.label}
            </button>
          ))}
        </nav>
        <div className="sidebar-foot">
          <div>Backend-first platform</div>
          <div>API v1 · Persian RTL</div>
          <div style={{ direction: "ltr", textAlign: "right" }}>v0.1.0</div>
        </div>
      </aside>

      <main className="main">
        <div className="topbar">
          <div>
            <div className="eyebrow">VANDAD / CONTROL PLANE</div>
            <h1>{nav.find((item) => item.id === tab)?.label}</h1>
            <div className="subhead">مدیریت Agentها، اجرای وظایف، دانش شرکت و تولید محتوا</div>
          </div>
          <div className="top-actions">
            <span className="pill">{companyId ? "شرکت انتخاب‌شده" : "شرکت پیش‌فرض"}</span>
            <button className="button ghost" onClick={() => void load()} disabled={loading}>{loading ? "در حال بروزرسانی…" : "↻ بروزرسانی"}</button>
          </div>
        </div>

        <div className="tabs-mobile">
          {nav.map((item) => <button key={item.id} className={tab === item.id ? "active" : ""} onClick={() => setTab(item.id)}>{item.label}</button>)}
        </div>

        {error && <div className="notice error" style={{ marginBottom: 14 }}>{error}</div>}
        {notice && <div className="notice" style={{ marginBottom: 14 }}>{notice}</div>}

        {tab === "overview" && (
          <>
            <div className="grid">
              <div className="card"><div className="metric-label">Agentهای فعال</div><div className="metric-value">{activeCount}</div><div className="metric-note">از {catalog.length} تعریف ثبت‌شده</div></div>
              <div className="card"><div className="metric-label">وظایف در حال اجرا</div><div className="metric-value">{runningCount}</div><div className="metric-note">Worker runtime</div></div>
              <div className="card"><div className="metric-label">تکمیل‌شده</div><div className="metric-value">{completedCount}</div><div className="metric-note">Taskهای ثبت‌شده</div></div>
              <div className="card"><div className="metric-label">نیازمند تأیید</div><div className="metric-value">{awaitingCount}</div><div className="metric-note">قبل از اجرای عملیات حساس</div></div>
            </div>
            <div className="two-col section">
              <div className="card">
                <div className="section-head"><h2>آخرین وظایف</h2><button className="button ghost" onClick={() => setTab("tasks")}>مشاهده همه</button></div>
                <div className="list">
                  {tasks.slice(0, 6).map((task) => <div className="row-item" key={task.id}><div><strong>{task.title}</strong><p>{task.agent_key} · {task.capability}</p></div><Status value={task.status} /></div>)}
                  {!tasks.length && <div className="empty">هنوز وظیفه‌ای ثبت نشده است.</div>}
                </div>
              </div>
              <div className="card">
                <div className="section-head"><h2>وضعیت زیرساخت</h2><span className="status active">API Connected</span></div>
                <div className="list">
                  <div className="row-item"><div><strong>Agent Catalog</strong><p>تعریف‌های code-owned</p></div><Status value="active" /></div>
                  <div className="row-item"><div><strong>Provider Registry</strong><p>{providers.length + sceneProviders.length} provider ثبت‌شده</p></div><Status value={providers.length ? "active" : "unknown"} /></div>
                  <div className="row-item"><div><strong>Company Brain</strong><p>{platforms.length} پلتفرم · {brands.length} برند</p></div><Status value="active" /></div>
                  <div className="row-item"><div><strong>Budget Policies</strong><p>{budgets.length} سیاست هزینه</p></div><Status value={budgets.length ? "active" : "unknown"} /></div>
                </div>
              </div>
            </div>
          </>
        )}

        {tab === "agents" && (
          <div className="section">
            <div className="section-head"><h2>کاتالوگ Agentها</h2><span className="muted small">تعریف‌ها در کد کنترل می‌شوند؛ شرکت فقط فعال‌سازی می‌کند.</span></div>
            <div className="grid">
              {catalog.map((agent) => {
                const enabled = companyAgents.find((item) => item.agent_key === agent.key);
                return <div className="card" key={agent.key}>
                  <div className="section-head"><h3>{agent.name}</h3><Status value={enabled?.status || "inactive"} /></div>
                  <p className="muted small">{agent.description}</p>
                  <div className="small muted" style={{ margin: "12px 0" }}>قابلیت‌ها</div>
                  <div className="list">{agent.capabilities.map((capability) => <div className="row-item" key={capability}><strong dir="ltr">{capability}</strong></div>)}</div>
                  <div className="actions" style={{ marginTop: 14 }}>
                    {!enabled ? <button className="button" onClick={() => void run(() => api(`/agents/${agent.key}`, { method: "PUT" }), "Agent برای شرکت فعال شد.")}>فعال‌سازی</button> :
                      <button className="button ghost" onClick={() => void run(() => api(`/agents/${agent.key}/status`, { method: "PATCH", body: JSON.stringify({ status: enabled.status === "active" ? "inactive" : "active" }) }), "وضعیت Agent تغییر کرد.")}>{enabled.status === "active" ? "غیرفعال‌سازی" : "فعال‌سازی مجدد"}</button>}
                    {agent.dispatch_ready && <span className="status active">آماده اجرا</span>}
                  </div>
                </div>;
              })}
              {!catalog.length && <div className="empty">کاتالوگ Agent دریافت نشد.</div>}
            </div>
          </div>
        )}

        {tab === "tasks" && (
          <div className="two-col section">
            <div className="card">
              <div className="section-head"><h2>وظایف شرکت</h2><span className="muted small">{tasks.length} مورد</span></div>
              <div className="list">
                {tasks.map((task) => <div className="row-item" key={task.id}>
                  <div style={{ minWidth: 0 }}>
                    <strong>{task.title}</strong><p>{task.agent_key} · {task.capability} · تلاش {task.attempt_count}/{task.max_attempts}</p>
                    <div className="actions" style={{ marginTop: 8 }}>
                      <button className="button ghost" onClick={() => void showEvents(task.id)}>رویدادها</button>
                      {task.status === "awaiting_approval" && <button className="button success" onClick={() => void run(() => api(`/agent-tasks/${task.id}/approve`, { method: "POST" }), "وظیفه تأیید و به صف ارسال شد.")}>تأیید</button>}
                      {["queued", "running", "pending", "awaiting_approval"].includes(task.status) && <button className="button danger" onClick={() => void run(() => api(`/agent-tasks/${task.id}/cancel`, { method: "POST" }), "درخواست لغو ثبت شد.")}>لغو</button>}
                      {task.status === "failed" && <button className="button" onClick={() => void run(() => api(`/agent-tasks/${task.id}/retry`, { method: "POST" }), "وظیفه دوباره به صف ارسال شد.")}>تلاش مجدد</button>}
                    </div>
                  </div>
                  <Status value={task.status} />
                </div>)}
                {!tasks.length && <div className="empty">وظیفه‌ای برای نمایش وجود ندارد.</div>}
              </div>
              {selectedTask && <div className="section">
                <div className="section-head"><h2>Timeline رویدادها</h2><button className="button ghost" onClick={() => void showEvents(selectedTask)}>↻</button></div>
                <div className="list">{events.map((event) => <div className="event" key={event.id}><strong>{event.sequence}. {event.event_type}</strong><p>{JSON.stringify(event.payload)}</p></div>)}{!events.length && <div className="empty">رویدادی ثبت نشده است.</div>}</div>
              </div>}
            </div>
            <div className="card">
              <h2 style={{ fontSize: 16, marginTop: 0 }}>ایجاد وظیفه جدید</h2>
              <form className="form-grid" onSubmit={createTask}>
                <div className="field full"><label>Agent</label><select value={taskAgent} onChange={(event) => setTaskAgent(event.target.value)}>{catalog.map((agent) => <option key={agent.key} value={agent.key}>{agent.name}</option>)}</select></div>
                <div className="field full"><label>Capability</label><select value={taskCapability} onChange={(event) => setTaskCapability(event.target.value)}>{(selectedDefinition?.capabilities || []).map((capability) => <option key={capability} value={capability}>{capability}</option>)}</select></div>
                <div className="field full"><label>عنوان</label><input value={taskTitle} onChange={(event) => setTaskTitle(event.target.value)} required /></div>
                <div className="field full"><label>Input Payload (JSON)</label><textarea value={taskPayload} onChange={(event) => setTaskPayload(event.target.value)} spellCheck={false} /></div>
                <div className="field full"><button className="button" type="submit">ثبت وظیفه</button></div>
              </form>
              <div className="notice" style={{ marginTop: 14 }}>برای Video Agent، وظیفه پس از ثبت در وضعیت «نیازمند تأیید» می‌ماند و تا تأیید انسانی اجرا نمی‌شود. شناسه پروژه و اطلاعات لازم باید در Payload قرار بگیرند.</div>
            </div>
          </div>
        )}

        {tab === "brain" && (
          <div className="two-col section">
            <div className="card"><div className="section-head"><h2>پلتفرم‌های متصل به دانش</h2><span className="pill">{platforms.length} پلتفرم</span></div>
              <div className="list">{platforms.map((platform) => <div className="row-item" key={platform.id}><div><strong>{platform.name}</strong><p dir="ltr">{platform.base_url || platform.platform_type}</p></div><Status value="active" /></div>)}{!platforms.length && <div className="empty">پلتفرمی ثبت نشده یا API در دسترس نیست.</div>}</div>
            </div>
            <div className="card"><div className="section-head"><h2>پروفایل‌های برند</h2><span className="pill">{brands.length} برند</span></div>
              <div className="list">{brands.map((brand) => <div className="row-item" key={brand.id}><div><strong>{brand.display_name || brand.name}</strong><p>{brand.industry || "زمینه فعالیت تعریف نشده"}</p></div><Status value={brand.status} /></div>)}{!brands.length && <div className="empty">برندی ثبت نشده است.</div>}</div>
              <div className="notice" style={{ marginTop: 14 }}>دانش پلتفرم باید از Source Documentهای کشف‌شده و Snapshotهای دارای Evidence ساخته شود؛ داده‌های بدون منبع نباید به‌عنوان حقیقت وارد سناریو شوند.</div>
            </div>
          </div>
        )}

        {tab === "content" && (
          <div className="section">
            <div className="two-col">
              <div className="card">
                <h2 style={{ fontSize: 16, marginTop: 0 }}>ایجاد پروژه محتوا</h2>
                <form className="form-grid" onSubmit={createContentProject}>
                  <div className="field full"><label>برند</label><select value={contentBrandId} onChange={(event) => setContentBrandId(event.target.value)} required><option value="">انتخاب برند</option>{brands.map((brand) => <option key={brand.id} value={brand.id}>{brand.display_name || brand.name}</option>)}</select></div>
                  <div className="field full"><label>مسیر تولید</label><select value={contentMode} onChange={(event) => setContentMode(event.target.value as "avatar" | "scene")}><option value="scene">Scene Video</option><option value="avatar">Talking Avatar</option></select></div>
                  <div className="field full"><label>موضوع ویدیو</label><textarea dir="rtl" value={contentTopic} onChange={(event) => setContentTopic(event.target.value)} required minLength={3} placeholder="موضوع، پیام اصلی و مخاطب ویدیو را بنویسید…" /></div>
                  {contentMode === "scene" && <div className="field full"><label>Scene Provider</label><select value={sceneProviderId} onChange={(event) => setSceneProviderId(event.target.value)}>{sceneProviders.map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.display_name}</option>)}</select></div>}
                  <div className="field full"><button className="button" type="submit">ایجاد پروژه</button></div>
                </form>
              </div>
              <div className="card">
                <h2 style={{ fontSize: 16, marginTop: 0 }}>اتصال‌های تولید</h2>
                <div className="field"><label>LLM Connection ID</label><input dir="ltr" value={llmConnectionId} onChange={(event) => setLlmConnectionId(event.target.value)} placeholder="UUID اتصال OpenAI-compatible" /></div>
                <div className="field" style={{ marginTop: 12 }}><label>HeyGen Connection ID</label><input dir="ltr" value={videoConnectionId} onChange={(event) => setVideoConnectionId(event.target.value)} placeholder="UUID اتصال HeyGen" /></div>
                <div className="notice" style={{ marginTop: 14 }}>تولید سناریو به سقف هزینه LLM نیاز دارد. تولید ویدیوی Scene یا Avatar پس از تأیید انسانی انجام می‌شود و برای Provider پولی به Budget شرکت نیاز دارد.</div>
              </div>
            </div>
            <div className="section-head section"><h2>پروژه‌های محتوا</h2><span className="pill">{projects.length} پروژه</span></div>
            <div className="list">
              {projects.map((project) => (
                <div className="row-item" key={project.id}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <strong>{project.topic}</strong>
                    <p dir="ltr">{project.video_mode} · {project.id}</p>
                    {project.error_message && <p>{project.error_message}</p>}
                    <div className="actions" style={{ marginTop: 8 }}>
                      {["draft", "script_failed"].includes(project.status) && <button className="button ghost" onClick={() => void generateProjectScript(project)}>{project.video_mode === "scene" ? "تولید سناریو" : "تولید Script"}</button>}
                      {project.status === "script_ready" && <button className="button success" onClick={() => void approveContentProject(project)}>تأیید سناریو</button>}
                      {project.status === "approved" && <button className="button" onClick={() => void generateProjectVideo(project)}>شروع تولید ویدیو</button>}
                    </div>
                  </div>
                  <Status value={project.status} />
                </div>
              ))}
              {!projects.length && <div className="empty">پروژه‌ای ثبت نشده است. از فرم بالا اولین پروژه را بسازید.</div>}
            </div>
            <div className="section-head section"><h2>برندها</h2><span className="muted small">{brands.length} برند</span></div>
            <div className="grid">
              {brands.map((brand) => <div className="card" key={brand.id}><div className="section-head"><h3>{brand.display_name || brand.name}</h3><Status value={brand.status} /></div><p className="muted small">{brand.industry || "صنعت تعریف نشده"}</p></div>)}
              {!brands.length && <div className="empty">اطلاعات برندها در دسترس نیست.</div>}
            </div>
          </div>
        )}

        {tab === "providers" && (
          <div className="two-col section">
            <div className="card"><div className="section-head"><h2>Provider Registry</h2><span className="pill">{providers.length} مورد</span></div>
              <div className="list">
                {providers.map((provider) => <div className="row-item" key={provider.provider_id}><div><strong>{provider.display_name}</strong><p dir="ltr">{provider.provider_id} · {provider.capabilities.join(", ")}</p></div><Status value="active" /></div>)}
                {sceneProviders.map((provider) => <div className="row-item" key={provider.provider_id}><div><strong>{provider.display_name}</strong><p dir="ltr">{provider.provider_id} · lip-sync: {provider.supports_lipsync ? "yes" : "no"} · reference image: {provider.supports_reference_image ? "yes" : "no"}</p></div><Status value="active" /></div>)}
              </div>
              <div className="section-head section"><h2>سلامت Provider · ۳۰ روز</h2><span className="muted small">{providerHealth.length} ارائه‌دهنده</span></div>
              <div className="list">{providerHealth.map((item) => <div className="row-item" key={item.provider_id}><div><strong dir="ltr">{item.provider_id}</strong><p>{item.calls_30d} درخواست · {item.errors_30d} خطا · موفقیت: {item.success_rate == null ? "—" : Math.round(item.success_rate * 100) + "%"} · میانگین تأخیر: {item.average_latency_ms == null ? "—" : Math.round(item.average_latency_ms) + " ms"}</p></div><Status value={item.errors_30d ? "unknown" : "settled"} /></div>)}{!providerHealth.length && <div className="empty">پس از اولین عملیات Provider، شاخص‌های سلامت ثبت‌شده نمایش داده می‌شوند.</div>}</div>
              <div className="section-head section"><h2>Usage Ledger</h2><span className="muted small">{usage.length} رکورد اخیر</span></div>
              <div className="list">{usage.slice(0, 10).map((item, index) => {
                const recordId = String(item.id || index);
                const status = String(item.status || "unknown");
                return <div className="row-item" key={recordId}>
                  <div style={{ minWidth: 0 }}>
                    <strong dir="ltr">{String(item.provider_id || "provider")}</strong>
                    <p>{String(item.operation || "")} · هزینه: {item.actual_cost_usd == null ? "نامشخص" : String(item.actual_cost_usd) + " USD"}</p>
                    {["unknown", "reserved"].includes(status) && <div className="actions" style={{ marginTop: 7 }}>
                      <input style={{ maxWidth: 120 }} type="number" min="0" step="0.000001" placeholder="هزینه USD" value={reconcileAmounts[recordId] || ""} onChange={(event) => setReconcileAmounts((current) => ({ ...current, [recordId]: event.target.value }))} />
                      <button className="button ghost" onClick={() => void reconcileUsage(recordId)}>تسویه دستی</button>
                    </div>}
                  </div>
                  <Status value={status} />
                </div>;
              })}{!usage.length && <div className="empty">هنوز Usage ثبت نشده است.</div>}</div>
            </div>
            <div className="card">
              <h2 style={{ fontSize: 16, marginTop: 0 }}>سیاست سقف هزینه</h2>
              <form className="form-grid" onSubmit={createBudget}>
                <div className="field full"><label>Provider ID</label><select value={budgetProvider} onChange={(event) => setBudgetProvider(event.target.value)}>{providers.map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.provider_id}</option>)}{sceneProviders.map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.provider_id}</option>)}</select></div>
                <div className="field"><label>سقف ماهانه (USD)</label><input type="number" min="0.000001" step="0.01" value={monthlyLimit} onChange={(event) => setMonthlyLimit(event.target.value)} required /></div>
                <div className="field"><label>سقف هر عملیات (USD)</label><input type="number" min="0.000001" step="0.01" value={operationLimit} onChange={(event) => setOperationLimit(event.target.value)} required /></div>
                <div className="field full"><button className="button" type="submit">ذخیره سقف هزینه</button></div>
              </form>
              <div className="section-head section"><h2>بودجه‌های ثبت‌شده</h2></div>
              <div className="list">{budgets.map((budget) => <div className="row-item" key={budget.provider_id}><div><strong dir="ltr">{budget.provider_id}</strong><p>{budget.monthly_limit_usd} USD ماهانه · {budget.per_operation_limit_usd} USD هر عملیات</p></div><Status value={budget.enabled ? "active" : "inactive"} /></div>)}{!budgets.length && <div className="empty">هنوز سقف هزینه‌ای تعریف نشده است.</div>}</div>
            </div>
          </div>
        )}

        {tab === "settings" && (
          <div className="card section" style={{ maxWidth: 680 }}>
            <h2 style={{ fontSize: 16, marginTop: 0 }}>تنظیمات اتصال</h2>
            <div className="field"><label>Company ID (اختیاری؛ خالی = شرکت پیش‌فرض Backend)</label><input dir="ltr" value={companyId} onChange={(event) => setCompanyId(event.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /></div>
            <div className="actions" style={{ marginTop: 14 }}><button className="button" onClick={saveCompany}>ذخیره و بارگذاری مجدد</button><button className="button ghost" onClick={() => { setCompanyId(""); window.localStorage.removeItem("vandad.companyId"); void load(); }}>استفاده از شرکت پیش‌فرض</button></div>
            <div className="notice section">آدرس API از متغیر NEXT_PUBLIC_API_BASE_URL خوانده می‌شود. در حالت توسعه مقدار پیش‌فرض http://localhost:8000/api/v1 است. احراز هویت کامل کاربران و نقش‌ها هنوز باید در Backend تکمیل شود؛ این UI جایگزین آن نیست.</div>
          </div>
        )}

        <div className="footer-note">Vandad Agent · Frontend API client · همه عملیات حساس باید در Backend مجدداً مجوزسنجی شوند.</div>
      </main>
    </div>
  );
}
