"use client";

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, apiUrl } from "@/lib/api";

type Tab = "overview" | "agents" | "tasks" | "brain" | "content" | "providers" | "settings";
type AgentDefinition = { key: string; name: string; description: string; capabilities: string[]; maturity: string; dispatch_ready: boolean };
type CompanyAgent = { agent_key: string; status: string; definition: AgentDefinition };
type Task = { id: string; agent_key: string; capability: string; title: string; status: string; created_at: string; attempt_count: number; max_attempts: number; error_message?: string | null };
type Provider = { provider_id: string; display_name: string; description: string; capabilities: string[] };
type ProviderHealth = { provider_id: string; calls_30d: number; errors_30d: number; pending_30d: number; success_rate: number | null; average_latency_ms: number | null; last_call_at: string };
type SceneProvider = { provider_id: string; display_name: string; requires_network: boolean; supports_lipsync: boolean; supports_reference_image: boolean; requires_reference_image?: boolean; supports_cancellation: boolean; daily_quota: number | null };
type Budget = { provider_id: string; monthly_limit_usd: string; per_operation_limit_usd: string; enabled: boolean };
type EventItem = { id: string; sequence: number; event_type: string; payload: Record<string, unknown>; created_at: string };
type Brand = { id: string; name: string; display_name?: string; industry?: string; status: string };
type ScenarioScene = { index?: number; scene_index?: number; title?: string; scene_type?: string; visual_prompt?: string; prompt?: string; camera_direction?: string; transition?: string; narration?: string; pronunciation_text?: string; on_screen_text?: string; subtitle_text?: string; duration_seconds?: number };
type Scenario = { title?: string; hook?: string; opening_hook?: string; summary?: string; visual_style?: string; scenes?: ScenarioScene[]; cta?: string; cta_pronunciation_text?: string; caption?: string; hashtags?: string[] };
type ContentProject = { id: string; brand_id: string; topic: string; status: string; video_mode: "avatar" | "scene"; error_message?: string | null; video_workflow_execution_id?: string | null; final_artifact_id?: string | null; scenario?: Scenario | null; script_text?: string | null; video_title?: string | null; call_to_action?: string | null; scene_options?: Record<string, unknown> | null };
type CharacterImage = { key: string; platform: string; name: string; valid: boolean; error?: string };
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
    script_generating: "در حال تولید سناریو", script_ready: "در انتظار تأیید سناریو",
    video_generation_pending: "در صف تولید ویدیو", video_generating: "در حال تولید ویدیو",
    video_generated: "ویدیو تولید شد", assembling: "در حال مونتاژ",
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
  const [videoConnectionId, setVideoConnectionId] = useState("");
  const [sceneProviderId, setSceneProviderId] = useState("apimaster");
  const [characterImages, setCharacterImages] = useState<CharacterImage[]>([]);
  const [characterKey, setCharacterKey] = useState("");
  const [selectedPlatformIds, setSelectedPlatformIds] = useState<string[]>([]);
  const [selectedContentProjectId, setSelectedContentProjectId] = useState<string | null>(null);
  const [selectedContentProject, setSelectedContentProject] = useState<ContentProject | null>(null);
  const [videoFeedback, setVideoFeedback] = useState("");
  const [videoPreviewUrl, setVideoPreviewUrl] = useState("");
  const videoPreviewProjectRef = useRef<string | null>(null);

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
      api<CharacterImage[]>("/content/character-images"),
    ] as const);
    const [catalogResult, agentsResult, tasksResult, providersResult, healthResult, sceneProvidersResult, budgetsResult, usageResult, brandsResult, projectsResult, platformsResult, characterImagesResult] = results;
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
    if (characterImagesResult.status === "fulfilled") setCharacterImages(characterImagesResult.value || []);
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

  useEffect(() => {
    let active = true;
    if (!contentBrandId) {
      setSelectedPlatformIds([]);
      return () => { active = false; };
    }
    api<{ id: string }[]>(`/content/brands/${contentBrandId}/platforms`)
      .then((linked) => {
        if (active) setSelectedPlatformIds(linked.map((item) => item.id));
      })
      .catch(() => {
        if (active) setSelectedPlatformIds([]);
      });
    return () => { active = false; };
  }, [contentBrandId]);

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
    setLoading(true);
    try {
      await action();
      setNotice(success);
      await load();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "عملیات انجام نشد.");
    } finally {
      setLoading(false);
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
      if (contentMode === "scene") {
        if (selectedPlatformIds.length === 0) {
          throw new Error("برای این برند حداقل یک پلتفرم مرجع انتخاب کنید.");
        }
        await api(`/content/brands/${contentBrandId}/platforms`, {
          method: "PUT",
          body: JSON.stringify({ platform_ids: selectedPlatformIds }),
        });
        setNotice("در حال رصد پلتفرم‌های انتخاب‌شده و بروزرسانی دانش تأییدشده…");
        await api(`/content/brands/${contentBrandId}/monitor`, {
          method: "POST",
          body: JSON.stringify({}),
        });
        setNotice("رصد و تحلیل تمام شد؛ در حال ارسال دانش تأییدشده برای ساخت سناریو…");
      }

      let project: ContentProject;
      if (contentMode === "avatar") {
        project = await api<ContentProject>("/content/projects", {
          method: "POST",
          body: JSON.stringify({ brand_id: contentBrandId, topic: contentTopic }),
        });
      } else {
        project = await api<ContentProject>("/content/scene-projects", {
          method: "POST",
          body: JSON.stringify({
            brand_id: contentBrandId,
            topic: contentTopic,
            target_duration_seconds: 30,
            provider_id: sceneProviderId || undefined,
            character_key: characterKey || undefined,
            generation_mode: characterKey ? "single_shot" : "multi_scene",
          }),
        });
        setSelectedContentProjectId(project.id);
        setSelectedContentProject(project);
        project = await api<ContentProject>(`/content/scene-projects/${project.id}/generate-scenario`, {
          method: "POST",
          body: JSON.stringify({}),
        });
      }
      setSelectedContentProjectId(project.id);
      setSelectedContentProject(project);
      setVideoPreviewUrl("");
      videoPreviewProjectRef.current = null;
      setContentTopic("");
    }, "رصد پلتفرم‌ها انجام شد و تولید سناریو به‌صورت خودکار آغاز شد.");
  };

  const generateProjectScript = async (project: ContentProject) => {
    await run(async () => {
      const prefix = project.video_mode === "scene" ? "/content/scene-projects" : "/content/projects";
      const action = project.video_mode === "scene" ? "generate-scenario" : "generate-script";
      await api(`${prefix}/${project.id}/${action}`, {
        method: "POST",
        body: JSON.stringify({}),
      });
    }, "درخواست تولید سناریو ثبت شد.");
  };

  const approveContentProject = async (project: ContentProject) => {
    await run(async () => {
      if (project.video_mode === "scene") {
        await api(`/content/scene-projects/${project.id}/approve-scenario`, {
          method: "POST",
          body: JSON.stringify({ approved: true }),
        });
        const started = await api<ContentProject>(`/content/scene-projects/${project.id}/generate-video`, {
          method: "POST",
          body: JSON.stringify({ provider_id: sceneProviderId || undefined }),
        });
        setSelectedContentProjectId(started.id);
        setSelectedContentProject(started);
      } else {
        await api(`/content/projects/${project.id}/approve-script`, {
          method: "POST",
          body: JSON.stringify({ approved: true }),
        });
      }
    }, "سناریو تأیید شد؛ تولید ویدیو بدون خروج از همین صفحه آغاز شد.");
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

  const rejectAndRegenerateVideo = async (project: ContentProject) => {
    await run(async () => {
      const revision = await api<ContentProject>(`/content/scene-projects/${project.id}/reject-video`, {
        method: "POST",
        body: JSON.stringify({ feedback: videoFeedback.trim() }),
      });
      setSelectedContentProjectId(revision.id);
      setSelectedContentProject(revision);
      setVideoFeedback("");
      setVideoPreviewUrl("");
      videoPreviewProjectRef.current = null;
    }, "نسخه قبلی رد شد و تولید نسخه جدید با همان سناریوی تأییدشده آغاز شد.");
  };

  const selectContentProject = async (projectId: string) => {
    try {
      const project = await api<ContentProject>(`/content/projects/${projectId}`);
      setSelectedContentProjectId(projectId);
      setSelectedContentProject(project);
      setVideoPreviewUrl("");
      videoPreviewProjectRef.current = null;
      setError("");
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "دریافت پروژه ناموفق بود.");
    }
  };

  useEffect(() => {
    if (!selectedContentProjectId) return;
    let active = true;
    const refresh = async () => {
      try {
        const project = await api<ContentProject>(`/content/projects/${selectedContentProjectId}`);
        if (!active) return;
        setSelectedContentProject(project);
        if (
          project.status === "completed" &&
          project.final_artifact_id &&
          videoPreviewProjectRef.current !== project.id
        ) {
          const headers = new Headers();
          if (companyId) headers.set("X-Company-ID", companyId);
          const response = await fetch(`${apiUrl}/content/videos/${project.id}/content`, {
            headers, credentials: "include", cache: "no-store",
          });
          if (!response.ok) throw new Error(`دریافت ویدیو ناموفق بود (${response.status})`);
          const blob = await response.blob();
          if (!active) return;
          const nextUrl = URL.createObjectURL(blob);
          setVideoPreviewUrl((previous) => {
            if (previous) URL.revokeObjectURL(previous);
            return nextUrl;
          });
          videoPreviewProjectRef.current = project.id;
        }
      } catch (cause) {
        if (active) setError(cause instanceof Error ? cause.message : "بروزرسانی وضعیت تولید ناموفق بود.");
      }
    };
    void refresh();
    const timer = window.setInterval(() => void refresh(), 3500);
    return () => {
      active = false;
      window.clearInterval(timer);
    };
  }, [selectedContentProjectId, companyId]);

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
          <div className="section content-workspace">
            <div className="content-intro">
              <div>
                <div className="eyebrow">HUMAN-IN-THE-LOOP PRODUCTION</div>
                <h2>استودیو تولید و بازبینی محتوا</h2>
                <p>از شروع تولید تا تأیید سناریو، مشاهده ویدیو و درخواست نسخه جایگزین؛ همه در همین صفحه.</p>
              </div>
              <span className="pill">{projects.length} پروژه ثبت‌شده</span>
            </div>

            <div className="card">
              <div className="section-head"><h2>شروع تولید جدید</h2><span className="muted small">یک بار ثبت؛ سپس انتظار برای تأیید انسانی</span></div>
              <form className="form-grid" onSubmit={createContentProject}>
                <div className="field">
                  <label>برند</label>
                  <select value={contentBrandId} onChange={(event) => setContentBrandId(event.target.value)} required>
                    <option value="">انتخاب برند</option>
                    {brands.map((brand) => <option key={brand.id} value={brand.id}>{brand.display_name || brand.name}</option>)}
                  </select>
                </div>
                <div className="field">
                  <label>Scene Provider</label>
                  <select value={sceneProviderId} onChange={(event) => setSceneProviderId(event.target.value)}>
                    {sceneProviders.filter((provider) => !characterKey || provider.supports_reference_image).map((provider) => <option key={provider.provider_id} value={provider.provider_id}>{provider.display_name}</option>)}
                  </select>
                </div>
                <div className="field full">
                  <label>پلتفرم‌های مرجع این برند</label>
                  <div className="platform-choice-list">
                    {platforms.map((platform) => (
                      <label className="platform-choice" key={platform.id}>
                        <input
                          type="checkbox"
                          checked={selectedPlatformIds.includes(platform.id)}
                          onChange={(event) => setSelectedPlatformIds((current) =>
                            event.target.checked
                              ? [...current, platform.id]
                              : current.filter((id) => id !== platform.id)
                          )}
                        />
                        <span>{platform.name}</span>
                        <small>{platform.platform_type || "platform"}</small>
                      </label>
                    ))}
                    {!platforms.length && <span className="muted small">ابتدا پلتفرم‌ها را در بخش اتصال‌ها ثبت کنید.</span>}
                  </div>
                  <span className="muted small">در شروع تولید، منابع عمومی این پلتفرم‌ها دوباره بررسی می‌شوند؛ اگر محتوایی تغییر نکرده باشد از آخرین دانش تأییدشده استفاده می‌شود.</span>
                </div>
                <div className="field full">
                  <label>موضوع، هدف و پیام ویدیو</label>
                  <textarea dir="rtl" value={contentTopic} onChange={(event) => setContentTopic(event.target.value)} required minLength={3} placeholder="موضوع، مخاطب هدف، پیام اصلی و نتیجه‌ای که از ویدیو می‌خواهید…" />
                </div>
                <div className="field">
                  <label>شخصیت / تصویر مرجع (اختیاری)</label>
                  <select value={characterKey} onChange={(event) => {
                    const nextKey = event.target.value;
                    setCharacterKey(nextKey);
                    if (nextKey) {
                      const referenceProvider = sceneProviders.find((provider) => provider.supports_reference_image);
                      if (referenceProvider) setSceneProviderId(referenceProvider.provider_id);
                    }
                  }}>
                    <option value="">بدون تصویر مرجع</option>
                    {characterImages.filter((item) => item.valid).map((item) => (
                      <option key={item.key} value={item.key}>{item.platform} · {item.name}</option>
                    ))}
                  </select>
                  <span className="muted small">تصویر انتخاب‌شده به‌عنوان مرجع اصلی ارسال می‌شود؛ در مسیر تولید ویدیو ویرایش تصویری انجام نمی‌شود.</span>
                </div>
                <div className="field">
                  <label>اتصال LLM برای تولید سناریو</label>
                  <p className="muted small">اتصال فعال LLM شرکت به‌صورت خودکار انتخاب می‌شود.</p>
                </div>
                <div className="field full actions">
                  <button className="button" type="submit" disabled={loading}>{loading ? "در حال اجرا…" : "شروع تولید و ساخت سناریو"}</button>
                </div>
              </form>
            </div>

            <div className="content-review-grid section">
              <div className="card">
                <div className="section-head"><h2>پروژه‌ها و نسخه‌ها</h2><span className="pill">{projects.length}</span></div>
                <div className="list">
                  {projects.map((project) => (
                    <button type="button" className={`content-project-row ${selectedContentProjectId === project.id ? "selected" : ""}`} key={project.id} onClick={() => void selectContentProject(project.id)}>
                      <span className="content-project-main">
                        <strong>{project.video_title || project.topic}</strong>
                        <small dir="ltr">{project.video_mode} · {project.scene_options?.revision_number ? `نسخه ${String(project.scene_options.revision_number)}` : "نسخه اصلی"} · {project.id.slice(0, 8)}</small>
                      </span>
                      <Status value={project.status} />
                    </button>
                  ))}
                  {!projects.length && <div className="empty">هنوز پروژه‌ای ایجاد نشده است.</div>}
                </div>
              </div>

              <div className="card content-review-panel">
                {!selectedContentProject && (
                  <div className="empty">برای مشاهده سناریو یا ویدیو، یک پروژه را انتخاب کنید یا تولید جدید را شروع کنید.</div>
                )}
                {selectedContentProject && (
                  <>
                    <div className="section-head">
                      <div><h2>{selectedContentProject.video_title || selectedContentProject.topic}</h2><p className="muted small" dir="ltr">{selectedContentProject.id}</p></div>
                      <Status value={selectedContentProject.status} />
                    </div>
                    {selectedContentProject.error_message && <div className="notice error">{selectedContentProject.error_message}</div>}

                    {selectedContentProject.scenario && (
                      <div className="scenario-review">
                        <h3>سناریو برای تأیید شما</h3>
                        {selectedContentProject.scenario.title && <h4>{selectedContentProject.scenario.title}</h4>}
                        {(selectedContentProject.scenario.opening_hook || selectedContentProject.scenario.hook) && <p><strong>قلاب شروع:</strong> {selectedContentProject.scenario.opening_hook || selectedContentProject.scenario.hook}</p>}
                        {selectedContentProject.scenario.summary && <p>{selectedContentProject.scenario.summary}</p>}
                        <div className="scene-review-list">
                          {(selectedContentProject.scenario.scenes || []).map((scene, index) => (
                            <article className="scene-review-item" key={index}>
                              <div className="scene-review-heading"><strong>صحنه {index + 1}{scene.title ? ` · ${scene.title}` : ""}</strong><span>{scene.duration_seconds ? `${scene.duration_seconds} ثانیه` : ""}</span></div>
                              {scene.visual_prompt || scene.prompt ? <p><strong>تصویر:</strong> {scene.visual_prompt || scene.prompt}</p> : null}
                              {scene.narration && <p><strong>گویندگی:</strong> {scene.narration}</p>}
                              {scene.pronunciation_text && <p><strong>متن تلفظ با اعراب:</strong> {scene.pronunciation_text}</p>}
                              {scene.camera_direction && <p><strong>حرکت دوربین:</strong> {scene.camera_direction}</p>}
                              {scene.transition && <p><strong>گذار:</strong> {scene.transition}</p>}
                              {scene.subtitle_text && <p><strong>زیرنویس:</strong> {scene.subtitle_text}</p>}
                              {scene.on_screen_text && <p><strong>متن روی تصویر:</strong> {scene.on_screen_text}</p>}
                            </article>
                          ))}
                        </div>
                        {selectedContentProject.scenario.cta && <p><strong>دعوت به اقدام:</strong> {selectedContentProject.scenario.cta}</p>}
                        {selectedContentProject.scenario.cta_pronunciation_text && <p><strong>تلفظ CTA:</strong> {selectedContentProject.scenario.cta_pronunciation_text}</p>}
                        {selectedContentProject.scenario.caption && <p><strong>کپشن:</strong> {selectedContentProject.scenario.caption}</p>}
                        {selectedContentProject.scenario.hashtags?.length ? <p className="muted">{selectedContentProject.scenario.hashtags.join(" ")}</p> : null}
                      </div>
                    )}

                    {["script_generating", "video_generation_pending", "video_generating", "assembling"].includes(selectedContentProject.status) && (
                      <div className="notice production-progress">
                        {selectedContentProject.status === "script_generating" ? "در حال تحلیل و تولید سناریو با مدل زبانی…" : "در حال تولید، دریافت و آماده‌سازی ویدیو؛ وضعیت به‌صورت خودکار بروزرسانی می‌شود…"}
                      </div>
                    )}

                    {selectedContentProject.status === "script_failed" && (
                      <div className="human-gate">
                        <p>تولید سناریو ناموفق بود؛ می‌توانید از همین‌جا دوباره تلاش کنید.</p>
                        <button className="button ghost" onClick={() => void generateProjectScript(selectedContentProject)}>تلاش مجدد برای تولید سناریو</button>
                      </div>
                    )}
                    {selectedContentProject.status === "script_ready" && (
                      <div className="human-gate">
                        <p>سناریو آماده است. قبل از تأیید شما هیچ درخواست تولید ویدیویی ارسال نمی‌شود.</p>
                        <div className="actions">
                          <button className="button success" disabled={loading} onClick={() => void approveContentProject(selectedContentProject)}>تأیید سناریو و شروع تولید ویدیو</button>
                          
                        </div>
                      </div>
                    )}

                    {selectedContentProject.video_mode === "scene" && selectedContentProject.status === "approved" && (
                      <div className="human-gate">
                        <p>سناریو تأیید شده است اما تولید ویدیو هنوز شروع نشده؛ می‌توانید ارسال را دوباره انجام دهید.</p>
                        <button className="button" disabled={loading} onClick={() => void generateProjectVideo(selectedContentProject)}>شروع تولید ویدیو</button>
                      </div>
                    )}
                    {selectedContentProject.status === "completed" && (
                      <div className="video-review">
                        <h3>بازبینی ویدیوی تولیدشده</h3>
                        {videoPreviewUrl ? <video className="content-video-player" src={videoPreviewUrl} controls playsInline /> : <div className="notice">در حال آماده‌سازی پخش ویدیو…</div>}
                        {videoPreviewUrl && <a className="button ghost" href={videoPreviewUrl} download={`vandad-${selectedContentProject.id}.mp4`}>دریافت فایل ویدیو</a>}
                        <div className="human-gate reject-gate">
                          <label>اگر ویدیو مناسب نیست، دلیل یا اصلاح موردنظر را بنویسید (اختیاری)</label>
                          <textarea dir="rtl" value={videoFeedback} onChange={(event) => setVideoFeedback(event.target.value)} placeholder="مثلاً صحنه دوم تکراری است، حرکت دوربین طبیعی نیست، شخصیت باید واضح‌تر باشد…" />
                          <button className="button danger" disabled={loading} onClick={() => void rejectAndRegenerateVideo(selectedContentProject)}>رد ویدیو و تولید نسخه جدید</button>
                          <p className="muted small">نسخه فعلی حذف نمی‌شود؛ نسخه جدید با شناسه و تاریخچه مستقل ساخته می‌شود و سناریوی تأییدشده حفظ خواهد شد.</p>
                        </div>
                      </div>
                    )}
                    {selectedContentProject.status === "failed" && (
                      <div className="human-gate"><p>این تلاش ناموفق بود. خطا را بررسی کنید یا پروژه را دوباره انتخاب کنید.</p></div>
                    )}
                  </>
                )}
              </div>
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
              <div className="list">{providerHealth.map((item) => <div className="row-item" key={item.provider_id}><div><strong dir="ltr">{item.provider_id}</strong><p>{item.calls_30d} درخواست · {item.errors_30d} خطا · {item.pending_30d} در انتظار تعیین تکلیف · موفقیت: {item.success_rate == null ? "—" : Math.round(item.success_rate * 100) + "%"} · میانگین تأخیر: {item.average_latency_ms == null ? "—" : Math.round(item.average_latency_ms) + " ms"}</p></div><Status value={item.errors_30d ? "unknown" : "settled"} /></div>)}{!providerHealth.length && <div className="empty">پس از اولین عملیات Provider، شاخص‌های سلامت ثبت‌شده نمایش داده می‌شوند.</div>}</div>
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
            <div className="field"><label>Company ID (خالی = عضویت تک‌شرکتی در Production یا شرکت پیش‌فرض در توسعه)</label><input dir="ltr" value={companyId} onChange={(event) => setCompanyId(event.target.value)} placeholder="xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx" /></div>
            <div className="actions" style={{ marginTop: 14 }}><button className="button" onClick={saveCompany}>ذخیره و بارگذاری مجدد</button><button className="button ghost" onClick={() => { setCompanyId(""); window.localStorage.removeItem("vandad.companyId"); void load(); }}>استفاده از شرکت پیش‌فرض</button></div>
            <div className="notice section">آدرس API از متغیر NEXT_PUBLIC_API_BASE_URL خوانده می‌شود. در حالت توسعه مقدار پیش‌فرض http://localhost:8000/api/v1 است. احراز هویت Production از طریق OIDC Identity Proxy انجام می‌شود؛ Backend نقش و عضویت شرکت را مجدداً بررسی می‌کند.</div>
          </div>
        )}

        <div className="footer-note">Vandad Agent · Frontend API client · همه عملیات حساس باید در Backend مجدداً مجوزسنجی شوند.</div>
      </main>
    </div>
  );
}
