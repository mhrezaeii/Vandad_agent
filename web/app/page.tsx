"use client";

import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/lib/api";

type Tab = "overview" | "agents" | "tasks" | "brain" | "content" | "providers" | "settings";
type AgentDefinition = { key: string; name: string; description: string; capabilities: string[]; maturity: string; dispatch_ready: boolean };
type CompanyAgent = { agent_key: string; status: string; definition: AgentDefinition };
type Task = { id: string; agent_key: string; capability: string; title: string; status: string; created_at: string; attempt_count: number; max_attempts: number; error_message?: string | null };
type Provider = { provider_id: string; display_name: string; description: string; capabilities: string[] };
type SceneProvider = { provider_id: string; display_name: string; requires_network: boolean; supports_lipsync: boolean; supports_reference_image: boolean; supports_cancellation: boolean; daily_quota: number | null };
type Budget = { provider_id: string; monthly_limit_usd: string; per_operation_limit_usd: string; enabled: boolean };
type EventItem = { id: string; sequence: number; event_type: string; payload: Record<string, unknown>; created_at: string };
type Brand = { id: string; name: string; display_name?: string; industry?: string; status: string };
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
  const [sceneProviders, setSceneProviders] = useState<SceneProvider[]>([]);
  const [budgets, setBudgets] = useState<Budget[]>([]);
  const [usage, setUsage] = useState<Record<string, unknown>[]>([]);
  const [brands, setBrands] = useState<Brand[]>([]);
  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [events, setEvents] = useState<EventItem[]>([]);
  const [selectedTask, setSelectedTask] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [taskAgent, setTaskAgent] = useState("monitoring");
  const [taskCapability, setTaskCapability] = useState("platform.monitor");
  const [taskTitle, setTaskTitle] = useState("رصد و تحلیل پلتفرم");
  const [taskPayload, setTaskPayload] = useState('{"platform_id": ""}');
  const [budgetProvider, setBudgetProvider] = useState("llm_openai");
  const [monthlyLimit, setMonthlyLimit] = useState("20");
  const [operationLimit, setOperationLimit] = useState("2");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    const results = await Promise.allSettled([
      api<{ items: AgentDefinition[] }>("/agents/catalog"),
      api<CompanyAgent[]>("/agents"),
      api<{ items: Task[] }>("/agent-tasks?limit=50"),
      api<Provider[]>("/providers"),
      api<SceneProvider[]>("/providers/scene-video"),
      api<Budget[]>("/providers/budgets"),
      api<Record<string, unknown>[]>("/providers/usage?limit=50"),
      api<Brand[]>("/content/brands"),
      api<Platform[]>("/platforms"),
    ] as const);
    const [catalogResult, agentsResult, tasksResult, providersResult, sceneProvidersResult, budgetsResult, usageResult, brandsResult, platformsResult] = results;
    if (catalogResult.status === "fulfilled") setCatalog(catalogResult.value.items || []);
    if (agentsResult.status === "fulfilled") setCompanyAgents(agentsResult.value || []);
    if (tasksResult.status === "fulfilled") setTasks(tasksResult.value.items || []);
    if (providersResult.status === "fulfilled") setProviders(providersResult.value || []);
    if (sceneProvidersResult.status === "fulfilled") setSceneProviders(sceneProvidersResult.value || []);
    if (budgetsResult.status === "fulfilled") setBudgets(budgetsResult.value || []);
    if (usageResult.status === "fulfilled") setUsage(usageResult.value || []);
    if (brandsResult.status === "fulfilled") setBrands(brandsResult.value || []);
    if (platformsResult.status === "fulfilled") setPlatforms(platformsResult.value || []);
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
            <div className="section-head"><h2>Content Studio</h2><span className="muted small">Brand context و پروژه‌های محتوا</span></div>
            <div className="grid">
              {brands.map((brand) => <div className="card" key={brand.id}><div className="section-head"><h3>{brand.display_name || brand.name}</h3><Status value={brand.status} /></div><p className="muted small">{brand.industry || "صنعت تعریف نشده"}</p><div className="small muted" dir="ltr">{brand.id}</div></div>)}
              {!brands.length && <div className="empty">اطلاعات برندها در دسترس نیست.</div>}
            </div>
            <div className="notice section">چرخه تولید در Backend شامل ساخت سناریو، تأیید انسانی، انتخاب مسیر Avatar یا Scene و ثبت Artifact است. تولید ویدیوی پولی بدون Approval و Budget مجاز نیست.</div>
          </div>
        )}

        {tab === "providers" && (
          <div className="two-col section">
            <div className="card"><div className="section-head"><h2>Provider Registry</h2><span className="pill">{providers.length} مورد</span></div>
              <div className="list">
                {providers.map((provider) => <div className="row-item" key={provider.provider_id}><div><strong>{provider.display_name}</strong><p dir="ltr">{provider.provider_id} · {provider.capabilities.join(", ")}</p></div><Status value="active" /></div>)}
                {sceneProviders.map((provider) => <div className="row-item" key={provider.provider_id}><div><strong>{provider.display_name}</strong><p dir="ltr">{provider.provider_id} · lip-sync: {provider.supports_lipsync ? "yes" : "no"} · reference image: {provider.supports_reference_image ? "yes" : "no"}</p></div><Status value="active" /></div>)}
              </div>
              <div className="section-head section"><h2>Usage Ledger</h2><span className="muted small">{usage.length} رکورد اخیر</span></div>
              <div className="list">{usage.slice(0, 10).map((item, index) => <div className="row-item" key={String(item.id || index)}><div><strong dir="ltr">{String(item.provider_id || "provider")}</strong><p>{String(item.operation || "")} · هزینه: {item.actual_cost_usd == null ? "نامشخص" : String(item.actual_cost_usd) + " USD"}</p></div><Status value={String(item.status || "unknown")} /></div>)}{!usage.length && <div className="empty">هنوز Usage ثبت نشده است.</div>}</div>
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
