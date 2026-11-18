"use client";

import { useCallback, useEffect, useMemo, useState, type FormEvent } from "react";
import { api, errorMessage } from "@/lib/api";
import { useAuth } from "@/lib/auth";
import { useI18n } from "@/lib/i18n";
import { ACTIVE_PROJECT_STATUSES, useProduction } from "@/lib/use-production";
import type {
  AccountList,
  AuthUser,
  Brand,
  CharacterImage,
  ContentProject,
  SceneProvider,
  WorkflowStep,
} from "@/lib/types";
import {
  Alert,
  BrandMark,
  CopyField,
  Field,
  LocaleSwitch,
  Modal,
  Spinner,
  StatusPill,
  cx,
  statusTone,
} from "@/components/ui";

const BRAND_KEY = "vandad.brandId";

type ScenarioSceneView = {
  index?: number;
  narration?: string;
  on_screen_text?: string | null;
  duration_seconds?: number;
  scene_type?: string;
};

type ScenarioView = {
  title?: string;
  opening_hook?: string;
  scenes?: ScenarioSceneView[];
  cta?: string;
  caption?: string;
  hashtags?: string[];
  total_duration_seconds?: number;
};

const HIDDEN_METADATA =
  /(path|secret|token|password|credential|api[_-]?key|url|uri|sha256|artifact|reservation)/i;

function safeMetadata(
  metadata: Record<string, unknown> | null | undefined,
): Array<[string, string]> {
  if (!metadata) return [];
  return Object.entries(metadata)
    .filter(
      ([key, value]) =>
        !HIDDEN_METADATA.test(key) &&
        (typeof value === "string" ||
          typeof value === "number" ||
          typeof value === "boolean"),
    )
    .map(([key, value]) => [key, String(value)]);
}

function formatCost(metadata: Record<string, unknown> | null | undefined) {
  if (!metadata) return null;
  for (const key of [
    "reported_scene_cost_usd",
    "estimated_scene_cost_usd",
    "total_cost_usd",
    "estimated_cost_usd",
  ]) {
    const raw = metadata[key];
    const amount =
      typeof raw === "number" ? raw : typeof raw === "string" ? Number(raw) : NaN;
    if (Number.isFinite(amount)) return `$${amount.toFixed(2)}`;
  }
  return null;
}

function readScenario(project: ContentProject | null): ScenarioView | null {
  if (!project?.scenario) return null;
  return project.scenario as unknown as ScenarioView;
}

function initials(value: string) {
  const parts = value.trim().split(/\s+/).filter(Boolean);
  if (parts.length === 0) return "؟";
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return `${parts[0][0]}${parts[1][0]}`.toUpperCase();
}

function TimelineStep({
  step,
  last,
}: {
  step: WorkflowStep;
  last: boolean;
}) {
  const { t, stepStatus, nodeLabel, formatNumber } = useI18n();
  const duration =
    step.duration_ms != null
      ? `${formatNumber(Math.round(step.duration_ms / 100) / 10)} ${t("seconds")}`
      : null;
  return (
    <li className={cx("timeline-step", `tone-${statusTone(step.status)}`)}>
      <span className="timeline-rail" aria-hidden="true">
        <span className="timeline-dot" />
        {!last ? <span className="timeline-line" /> : null}
      </span>
      <div className="timeline-body">
        <div className="timeline-head">
          <strong>{nodeLabel(step.node_key)}</strong>
          <StatusPill status={step.status} label={stepStatus(step.status)} />
        </div>
        <div className="timeline-meta">
          <span dir="ltr">{step.node_type}</span>
          <span>
            {t("stepAttempt")} {formatNumber(step.attempt)}
          </span>
          {duration ? (
            <span>
              {t("stepDuration")} {duration}
            </span>
          ) : null}
        </div>
        {step.error_message ? (
          <p className="timeline-error">{step.error_message}</p>
        ) : null}
      </div>
    </li>
  );
}

export function Dashboard() {
  const { t, locale, formatNumber, formatDateTime, projectStatus, executionStatus } =
    useI18n();
  const { user, logout } = useAuth();
  const production = useProduction();
  const {
    project,
    execution,
    steps,
    executionKind,
    videoUrl,
    error: productionError,
    busy: productionBusy,
    cancelling,
    restoring,
    start,
    cancel,
    retryScenario,
    reset,
    refresh,
    selectExecution,
    setError: setProductionError,
  } = production;

  const [brands, setBrands] = useState<Brand[]>([]);
  const [brandsLoading, setBrandsLoading] = useState(true);
  const [brandsError, setBrandsError] = useState<string | null>(null);
  const [selectedBrandId, setSelectedBrandId] = useState<string | null>(null);

  const [characters, setCharacters] = useState<CharacterImage[]>([]);
  const [providers, setProviders] = useState<SceneProvider[]>([]);
  const [optionsLoading, setOptionsLoading] = useState(true);

  const [topic, setTopic] = useState("");
  const [duration, setDuration] = useState(20);
  const [providerId, setProviderId] = useState("");
  const [characterKey, setCharacterKey] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [addOpen, setAddOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [newIndustry, setNewIndustry] = useState("");
  const [newDomain, setNewDomain] = useState("");
  const [newTone, setNewTone] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [adminOpen, setAdminOpen] = useState(false);
  const [adminUsers, setAdminUsers] = useState<AuthUser[]>([]);
  const [adminLoading, setAdminLoading] = useState(false);
  const [adminError, setAdminError] = useState<string | null>(null);
  const [decisionId, setDecisionId] = useState<string | null>(null);
  const [pendingCount, setPendingCount] = useState(0);

  const [videoInfo, setVideoInfo] = useState<{
    width: number;
    height: number;
    duration: number;
  } | null>(null);

  const selectBrand = useCallback((id: string) => {
    setSelectedBrandId(id);
    window.localStorage.setItem(BRAND_KEY, id);
  }, []);

  const loadBrands = useCallback(async () => {
    setBrandsLoading(true);
    setBrandsError(null);
    try {
      const list = await api<Brand[]>("/content/brands");
      setBrands(list);
      const saved = window.localStorage.getItem(BRAND_KEY);
      setSelectedBrandId((current) => {
        if (current && list.some((brand) => brand.id === current)) return current;
        if (saved && list.some((brand) => brand.id === saved)) return saved;
        return list[0]?.id ?? null;
      });
    } catch (cause) {
      setBrandsError(errorMessage(cause));
    } finally {
      setBrandsLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadBrands();
  }, [loadBrands]);

  useEffect(() => {
    let active = true;
    (async () => {
      const [characterResult, providerResult] = await Promise.allSettled([
        api<CharacterImage[]>("/content/character-images"),
        api<SceneProvider[]>("/content/scene-providers"),
      ]);
      if (!active) return;
      if (characterResult.status === "fulfilled") {
        const valid = characterResult.value.filter((item) => item.valid);
        setCharacters(valid);
        setCharacterKey((current) => current || valid[0]?.key || "");
      }
      if (providerResult.status === "fulfilled") {
        setProviders(providerResult.value);
      }
      setOptionsLoading(false);
    })();
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    setVideoInfo(null);
  }, [videoUrl]);

  const loadPending = useCallback(async () => {
    setAdminLoading(true);
    setAdminError(null);
    try {
      const result = await api<AccountList>(
        "/auth/users?status=pending_approval&limit=50",
      );
      setAdminUsers(result.items);
      setPendingCount(result.pending);
    } catch (cause) {
      setAdminError(errorMessage(cause));
    } finally {
      setAdminLoading(false);
    }
  }, []);

  useEffect(() => {
    if (user?.role !== "admin") return;
    void api<AccountList>("/auth/users?status=pending_approval&limit=1")
      .then((result) => setPendingCount(result.pending))
      .catch(() => undefined);
  }, [user?.role]);

  const selectedBrand = useMemo(
    () => brands.find((brand) => brand.id === selectedBrandId) ?? null,
    [brands, selectedBrandId],
  );
  const projectBrand = useMemo(
    () => (project ? brands.find((brand) => brand.id === project.brand_id) ?? null : null),
    [brands, project],
  );

  const scenario = readScenario(project);
  const scenes = scenario?.scenes ?? [];
  const scenarioDuration =
    scenario?.total_duration_seconds ??
    scenes.reduce((total, scene) => total + (scene.duration_seconds ?? 0), 0);
  const isActive = project ? ACTIVE_PROJECT_STATUSES.has(project.status) : false;
  const isCompleted = project?.status === "completed";
  const scenarioApproved =
    !!project &&
    [
      "approved",
      "scenes_prepared",
      "video_generation_pending",
      "video_generating",
      "video_generated",
      "assembling",
      "completed",
    ].includes(project.status);
  const canCancel =
    !!project && !["completed", "cancelled"].includes(project.status);
  const canRetryScenario =
    project?.status === "draft" || project?.status === "script_failed";

  const providerLabel =
    providers.find((item) => item.provider_id === project?.scene_video_provider)
      ?.display_name ??
    project?.scene_video_provider ??
    null;

  const startProcess = async () => {
    setFormError(null);
    setNotice(null);
    if (!selectedBrandId) {
      setFormError(t("noProfileSelected"));
      return;
    }
    if (topic.trim().length < 3) {
      setFormError(t("topicTooShort"));
      return;
    }
    if (!Number.isFinite(duration) || duration < 5 || duration > 120) {
      setFormError(t("durationRange"));
      return;
    }
    if (!characterKey) {
      setFormError(t("noCharacterSelected"));
      return;
    }
    try {
      await start({
        brandId: selectedBrandId,
        topic: topic.trim(),
        durationSeconds: duration,
        providerId: providerId || null,
        characterKey,
      });
      setNotice(t("autoApproveNote"));
    } catch (cause) {
      if (cause instanceof Error && cause.message === "duplicate-submission") {
        setFormError(t("duplicateSubmission"));
      } else {
        setFormError(errorMessage(cause));
      }
    }
  };

  const createProfile = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCreateError(null);
    if (newName.trim().length === 0) {
      setCreateError(t("required"));
      return;
    }
    setCreating(true);
    try {
      const created = await api<Brand>("/content/brands", {
        method: "POST",
        body: JSON.stringify({
          name: newName.trim(),
          display_name: newDisplayName.trim() || null,
          tone: newTone.trim() || null,
          industry: newIndustry.trim() || null,
          domain: newDomain.trim() || null,
          description: newDescription.trim() || null,
          language: locale === "fa" ? "fa-IR" : "en-US",
        }),
      });
      setBrands((previous) => [...previous, created]);
      selectBrand(created.id);
      setAddOpen(false);
      setNewName("");
      setNewDisplayName("");
      setNewIndustry("");
      setNewDomain("");
      setNewTone("");
      setNewDescription("");
    } catch (cause) {
      setCreateError(errorMessage(cause));
    } finally {
      setCreating(false);
    }
  };

  const decide = async (userId: string, approved: boolean) => {
    setDecisionId(userId);
    setAdminError(null);
    try {
      await api(
        `/auth/users/${userId}/${approved ? "approve" : "reject"}`,
        { method: "POST" },
      );
      setAdminUsers((previous) =>
        previous.filter((item) => item.id !== userId),
      );
      setPendingCount((count) => Math.max(0, count - 1));
      setNotice(t("decisionSaved"));
    } catch (cause) {
      setAdminError(errorMessage(cause));
    } finally {
      setDecisionId(null);
    }
  };

  const downloadVideo = () => {
    if (!videoUrl || !project) return;
    const safeTitle = (project.video_title || project.topic || "vandad")
      .replace(/[\\/:*?"<>|]+/g, "-")
      .slice(0, 80);
    const anchor = document.createElement("a");
    anchor.href = videoUrl;
    anchor.download = `${safeTitle || "vandad"}.mp4`;
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
  };

  if (!user) return null;

  const metadataItems = project
    ? [
        { label: t("metaVideoTitle"), value: project.video_title },
        { label: t("metaCta"), value: project.call_to_action },
        { label: t("metaProvider"), value: providerLabel },
        {
          label: t("metaDuration"),
          value:
            videoInfo?.duration != null
              ? `${formatNumber(Math.round(videoInfo.duration * 10) / 10)} ${t("seconds")}`
              : scenarioDuration > 0
                ? `${formatNumber(Math.round(scenarioDuration * 10) / 10)} ${t("seconds")}`
                : null,
        },
        {
          label: t("metaResolution"),
          value: videoInfo
            ? `${formatNumber(videoInfo.width)}×${formatNumber(videoInfo.height)}`
            : projectBrand
              ? `${formatNumber(projectBrand.video_width)}×${formatNumber(projectBrand.video_height)}`
              : selectedBrand
                ? `${formatNumber(selectedBrand.video_width)}×${formatNumber(selectedBrand.video_height)}`
                : null,
        },
        { label: t("metaCost"), value: formatCost(project.cost_metadata) },
      ].filter((item) => Boolean(item.value))
    : [];

  const qcEntries = safeMetadata(project?.assembly_metadata);

  return (
    <div className="app-shell">
      <header className="dash-topbar glass">
        <div className="brand-lockup">
          <BrandMark />
          <div>
            <strong>{t("appName")}</strong>
            <small>{t("appTagline")}</small>
          </div>
        </div>

        <div className="dash-topbar-actions">
          {user.role === "admin" ? (
            <button
              type="button"
              className="button button--ghost approvals-button"
              onClick={() => {
                setAdminOpen(true);
                void loadPending();
              }}
            >
              {t("approvals")}
              {pendingCount > 0 ? (
                <span className="count-badge">{formatNumber(pendingCount)}</span>
              ) : null}
            </button>
          ) : null}
          <LocaleSwitch />
          <div className="user-chip">
            <span className="avatar">{initials(user.full_name)}</span>
            <div>
              <strong>{user.full_name}</strong>
              <small>
                {user.role === "admin" ? t("roleAdmin") : t("roleMember")}
              </small>
            </div>
          </div>
          <button
            type="button"
            className="icon-button"
            onClick={logout}
            title={t("logout")}
            aria-label={t("logout")}
          >
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none">
              <path
                d="M15 4h3a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-3M10 8l-4 4 4 4M6 12h9"
                stroke="currentColor"
                strokeWidth="1.7"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </svg>
          </button>
        </div>
      </header>

      <main className="dash-main">
        <div className="dash-intro">
          <div>
            <span className="eyebrow">{t("appTagline")}</span>
            <h1>
              {t("dashboardGreeting")}، {user.full_name.split(" ")[0]}
            </h1>
            <p>{t("dashboardSubtitle")}</p>
          </div>
          <div className="dash-intro-badges">
            <StatusPill status="approved" label={t("signedInAs")} />
            <span className="muted small" dir="ltr">
              {user.email}
            </span>
          </div>
        </div>

        {notice ? (
          <Alert tone="success" onClose={() => setNotice(null)}>
            {notice}
          </Alert>
        ) : null}

        <section className="glass card">
          <header className="card-head">
            <div>
              <h2>{t("profilesTitle")}</h2>
              <p>{t("profilesSubtitle")}</p>
            </div>
            <button
              type="button"
              className="button button--ghost"
              onClick={() => {
                setCreateError(null);
                setAddOpen(true);
              }}
            >
              <span aria-hidden="true">＋</span> {t("addProfile")}
            </button>
          </header>

          {brandsError ? (
            <Alert
              tone="error"
              onClose={() => setBrandsError(null)}
            >
              {brandsError}
            </Alert>
          ) : null}

          {brandsLoading ? (
            <div className="skeleton-grid">
              {[0, 1, 2].map((index) => (
                <div className="skeleton-card" key={index} />
              ))}
            </div>
          ) : brands.length === 0 ? (
            <div className="empty-state">
              <div className="empty-visual" aria-hidden="true">
                ✦
              </div>
              <strong>{t("noProfiles")}</strong>
              <p>{t("noProfilesHint")}</p>
              <button
                type="button"
                className="button button--primary"
                onClick={() => setAddOpen(true)}
              >
                {t("addProfile")}
              </button>
            </div>
          ) : (
            <div className="profile-grid">
              {brands.map((brand) => {
                const selected = brand.id === selectedBrandId;
                return (
                  <button
                    type="button"
                    key={brand.id}
                    className={cx("profile-card", selected && "is-selected")}
                    onClick={() => selectBrand(brand.id)}
                    aria-pressed={selected}
                  >
                    <span className="profile-avatar">
                      {initials(brand.display_name || brand.name)}
                    </span>
                    <span className="profile-info">
                      <strong>{brand.display_name || brand.name}</strong>
                      <span className="profile-key" dir="ltr">
                        {brand.name}
                      </span>
                      <small>
                        {[brand.industry, brand.domain]
                          .filter(Boolean)
                          .join(" · ") || t("unknown")}
                      </small>
                    </span>
                    {selected ? (
                      <span className="profile-check" aria-hidden="true">
                        ✓
                      </span>
                    ) : null}
                  </button>
                );
              })}
            </div>
          )}

          {selectedBrand ? (
            <div className="selected-profile">
              <div>
                <span>{t("brandDisplayName")}</span>
                <strong>{selectedBrand.display_name || selectedBrand.name}</strong>
              </div>
              <div>
                <span>{t("brandLanguage")}</span>
                <strong dir="ltr">{selectedBrand.language}</strong>
              </div>
              <div>
                <span>{t("brandIndustry")}</span>
                <strong>{selectedBrand.industry || "—"}</strong>
              </div>
              <div>
                <span>{t("brandDomain")}</span>
                <strong dir="ltr">{selectedBrand.domain || "—"}</strong>
              </div>
              <div>
                <span>{t("brandTone")}</span>
                <strong>{selectedBrand.tone || "—"}</strong>
              </div>
            </div>
          ) : null}
        </section>

        <section className="glass card production-card">
          <header className="card-head">
            <div>
              <h2>{t("productionTitle")}</h2>
              <p>{t("productionSubtitle")}</p>
            </div>
            {selectedBrand ? (
              <span className="pill pill--info">{selectedBrand.display_name || selectedBrand.name}</span>
            ) : null}
          </header>

          {formError ? (
            <Alert tone="error" onClose={() => setFormError(null)}>
              {formError}
            </Alert>
          ) : null}

          <div className="form-grid">
            <Field label={t("topicLabel")} htmlFor="topic" hint={t("productionSubtitle")}>
              <textarea
                id="topic"
                name="topic"
                dir="auto"
                rows={3}
                placeholder={t("topicPlaceholder")}
                value={topic}
                onChange={(event) => setTopic(event.target.value)}
                disabled={productionBusy || !!project}
              />
            </Field>

            <div className="form-row">
              <Field label={t("durationLabel")} htmlFor="duration">
                <input
                  id="duration"
                  name="duration"
                  type="number"
                  min={5}
                  max={120}
                  step={1}
                  value={duration}
                  onChange={(event) => {
                    const next = Number.parseInt(event.target.value, 10);
                    setDuration(Number.isFinite(next) ? next : 5);
                  }}
                  disabled={productionBusy || !!project}
                />
              </Field>

              <Field
                label={t("characterLabel")}
                htmlFor="character"
                hint={t("characterHint")}
              >
                <select
                  id="character"
                  name="character"
                  value={characterKey}
                  onChange={(event) => setCharacterKey(event.target.value)}
                  disabled={productionBusy || optionsLoading || !!project}
                >
                  <option value="">
                    {optionsLoading ? t("loading") : `${t("select")}…`}
                  </option>
                  {characters.map((character) => (
                    <option key={character.key} value={character.key}>
                      {character.name} — {character.platform}
                    </option>
                  ))}
                </select>
              </Field>

              <Field label={t("providerLabel")} htmlFor="provider">
                <select
                  id="provider"
                  name="provider"
                  value={providerId}
                  onChange={(event) => setProviderId(event.target.value)}
                  disabled={productionBusy || optionsLoading || !!project}
                >
                  <option value="">{t("providerAuto")}</option>
                  {providers.map((provider) => (
                    <option key={provider.provider_id} value={provider.provider_id}>
                      {provider.display_name}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </div>

          {!optionsLoading && characters.length === 0 ? (
            <p className="inline-note inline-note--warn">{t("noCharacters")}</p>
          ) : null}

          <div className="production-actions">
            <button
              type="button"
              className="button button--primary button--start"
              onClick={() => void startProcess()}
              disabled={
                productionBusy || restoring || !!project || characters.length === 0
              }
            >
              {productionBusy ? (
                <Spinner label={t("startingProcess")} />
              ) : (
                <>
                  <span className="start-icon" aria-hidden="true">
                    ▶
                  </span>
                  {t("startProcess")}
                </>
              )}
            </button>
            {project ? (
              <button
                type="button"
                className="button button--ghost"
                onClick={reset}
              >
                {t("newProcess")}
              </button>
            ) : null}
          </div>
          <p className="auto-note">
            <span aria-hidden="true">⚡</span> {t("autoApproveNote")}
          </p>
        </section>

        {restoring ? (
          <section className="glass card">
            <Spinner label={t("loading")} />
          </section>
        ) : project ? (
          <section className="glass card process-card">
            <header className="card-head">
              <div>
                <h2>{t("activeProcess")}</h2>
                <p>
                  {t("projectId")}:{" "}
                  <code dir="ltr">{project.id.slice(0, 8)}</code>
                </p>
              </div>
              <div className="process-head-actions">
                <StatusPill
                  status={project.status}
                  label={projectStatus(project.status)}
                />
                {canRetryScenario ? (
                  <button
                    type="button"
                    className="button button--ghost button--tiny"
                    onClick={() => void retryScenario()}
                    disabled={productionBusy}
                    title={t("retryHint")}
                  >
                    {t("retryScenario")}
                  </button>
                ) : null}
                {canCancel ? (
                  <button
                    type="button"
                    className="button button--danger button--tiny"
                    onClick={() => void cancel()}
                    disabled={cancelling}
                  >
                    {cancelling ? <Spinner /> : t("cancelWorkflow")}
                  </button>
                ) : null}
                <button
                  type="button"
                  className="icon-button"
                  onClick={() => void refresh()}
                  title={t("refresh")}
                  aria-label={t("refresh")}
                >
                  <svg viewBox="0 0 24 24" width="17" height="17" fill="none">
                    <path
                      d="M20 12a8 8 0 1 1-2.34-5.66M20 4v5h-5"
                      stroke="currentColor"
                      strokeWidth="1.7"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>
            </header>

            {productionError ? (
              <Alert
                tone="error"
                onClose={() => setProductionError(null)}
              >
                {productionError}
              </Alert>
            ) : null}

            <div className="process-ids">
              <CopyField label={t("workflowId")} value={execution?.id} />
              <CopyField label={t("projectId")} value={project.id} />
            </div>

            <div className="execution-bar">
              <div className="execution-summary">
                <span className="muted small">{t("openWorkflow")}</span>
                <strong dir="ltr">
                  {execution?.workflow_name ?? t("noWorkflowYet")}
                </strong>
                {execution ? (
                  <StatusPill
                    status={execution.status}
                    label={executionStatus(execution.status)}
                  />
                ) : null}
              </div>
              {project.script_workflow_execution_id &&
              project.video_workflow_execution_id ? (
                <div className="segmented segmented--small">
                  <button
                    type="button"
                    className={cx(executionKind === "script" && "is-active")}
                    onClick={() => selectExecution("script")}
                  >
                    {t("statusScenario")}
                  </button>
                  <button
                    type="button"
                    className={cx(executionKind === "video" && "is-active")}
                    onClick={() => selectExecution("video")}
                  >
                    {t("statusVideo")}
                  </button>
                </div>
              ) : null}
            </div>

            {execution?.error_message ? (
              <Alert tone="error">{execution.error_message}</Alert>
            ) : null}
            {project.error_message ? (
              <Alert tone="error">{project.error_message}</Alert>
            ) : null}

            {steps.length === 0 ? (
              <p className="empty-inline">{t("timelineEmpty")}</p>
            ) : (
              <ol className="timeline">
                {steps.map((step, index) => (
                  <TimelineStep
                    key={step.id}
                    step={step}
                    last={index === steps.length - 1}
                  />
                ))}
              </ol>
            )}

            {isActive ? (
              <p className="live-note">
                <span className="live-dot" aria-hidden="true" />
                {t("statusVideo")} · {t("refresh")} · 3s
              </p>
            ) : null}
          </section>
        ) : (
          <section className="glass card process-empty">
            <div className="process-empty-visual" aria-hidden="true">
              <span className="orbit orbit--one" />
              <span className="orbit orbit--two" />
              <span className="orbit-core">◎</span>
            </div>
            <h2>{t("productionTitle")}</h2>
            <p>{t("timelineEmpty")}</p>
          </section>
        )}

        <div className="dash-columns">
          <section className="glass card">
            <header className="card-head">
              <h2>{t("scenarioTitle")}</h2>
              {scenarioApproved ? (
                <StatusPill status="approved" label={t("scenarioApproved")} />
              ) : null}
            </header>

            {!scenario ? (
              <p className="empty-inline">{t("scenarioEmpty")}</p>
            ) : (
              <>
                <h3 className="scenario-title">{scenario.title}</h3>
                {scenario.opening_hook ? (
                  <p className="scenario-hook">{scenario.opening_hook}</p>
                ) : null}

                <div className="scenario-stats">
                  <div>
                    <span>{t("scenesCount")}</span>
                    <strong>{formatNumber(scenes.length)}</strong>
                  </div>
                  <div>
                    <span>{t("metaDuration")}</span>
                    <strong>
                      {formatNumber(Math.round(scenarioDuration * 10) / 10)}{" "}
                      {t("seconds")}
                    </strong>
                  </div>
                </div>

                {scenes.length === 0 ? (
                  <p className="empty-inline">{t("noScenes")}</p>
                ) : (
                  <ol className="scene-list">
                    {scenes.map((scene, index) => (
                      <li className="scene-item" key={`${index}-${scene.scene_type ?? "scene"}`}>
                        <div className="scene-item-head">
                          <span className="scene-index">
                            {formatNumber(index + 1)}
                          </span>
                          <span className="scene-type" dir="ltr">
                            {scene.scene_type ?? "b_roll"}
                          </span>
                          <span className="scene-duration">
                            {formatNumber(
                              Math.round((scene.duration_seconds ?? 0) * 10) / 10,
                            )}{" "}
                            {t("seconds")}
                          </span>
                        </div>
                        <p className="scene-narration">{scene.narration}</p>
                        {scene.on_screen_text ? (
                          <p className="scene-on-screen">
                            «{scene.on_screen_text}»
                          </p>
                        ) : null}
                      </li>
                    ))}
                  </ol>
                )}

                {scenario.cta ? (
                  <div className="scenario-cta">
                    <span>{t("metaCta")}</span>
                    <strong>{scenario.cta}</strong>
                  </div>
                ) : null}

                {scenario.hashtags && scenario.hashtags.length > 0 ? (
                  <div className="hashtags">
                    {scenario.hashtags.map((tag) => (
                      <span key={tag} dir="rtl">
                        {tag}
                      </span>
                    ))}
                  </div>
                ) : null}
              </>
            )}
          </section>

          <section className="glass card">
            <header className="card-head">
              <h2>{t("videoTitle")}</h2>
              {isCompleted ? (
                <StatusPill status="completed" label={t("statusCompleted")} />
              ) : null}
            </header>

            {!isCompleted ? (
              <div className="video-placeholder">
                <span className="video-placeholder-icon" aria-hidden="true">
                  ▷
                </span>
                <p>{t("videoNotReady")}</p>
              </div>
            ) : (
              <>
                {videoUrl ? (
                  <video
                    className="video-player"
                    src={videoUrl}
                    controls
                    playsInline
                    preload="metadata"
                    onLoadedMetadata={(event) => {
                      const element = event.currentTarget;
                      setVideoInfo({
                        width: element.videoWidth,
                        height: element.videoHeight,
                        duration: Number.isFinite(element.duration)
                          ? element.duration
                          : 0,
                      });
                    }}
                  />
                ) : project?.final_artifact_id ? (
                  <div className="video-placeholder">
                    <Spinner label={t("loading")} />
                  </div>
                ) : (
                  <div className="video-placeholder">
                    <span className="video-placeholder-icon" aria-hidden="true">
                      ▷
                    </span>
                    <p>{t("videoUnavailable")}</p>
                  </div>
                )}

                <div className="video-actions">
                  <button
                    type="button"
                    className="button button--primary"
                    onClick={downloadVideo}
                    disabled={!videoUrl}
                  >
                    <span aria-hidden="true">⤓</span> {t("downloadVideo")}
                  </button>
                </div>

                {metadataItems.length > 0 ? (
                  <div className="meta-block">
                    <h3>{t("metadataTitle")}</h3>
                    <dl className="meta-grid">
                      {metadataItems.map((item) => (
                        <div key={item.label}>
                          <dt>{item.label}</dt>
                          <dd>{item.value}</dd>
                        </div>
                      ))}
                    </dl>
                  </div>
                ) : (
                  <p className="empty-inline">{t("videoMetaUnavailable")}</p>
                )}

                <div className="meta-block">
                  <h3>{t("qcTitle")}</h3>
                  {qcEntries.length === 0 ? (
                    <p className="empty-inline">{t("qcEmpty")}</p>
                  ) : (
                    <dl className="meta-grid">
                      {qcEntries.map(([key, value]) => (
                        <div key={key}>
                          <dt dir="ltr">{key.replaceAll("_", " ")}</dt>
                          <dd dir="auto">{value}</dd>
                        </div>
                      ))}
                    </dl>
                  )}
                </div>
              </>
            )}
          </section>
        </div>

        <footer className="footer-note">{t("footerNote")}</footer>
      </main>

      <Modal
        open={addOpen}
        title={t("addProfile")}
        subtitle={t("profilesSubtitle")}
        onClose={() => setAddOpen(false)}
        footer={
          <>
            <button
              type="button"
              className="button button--ghost"
              onClick={() => setAddOpen(false)}
              disabled={creating}
            >
              {t("cancel")}
            </button>
            <button
              type="submit"
              form="create-profile-form"
              className="button button--primary"
              disabled={creating}
            >
              {creating ? <Spinner label={t("creatingProfile")} /> : t("createProfile")}
            </button>
          </>
        }
      >
        <form id="create-profile-form" className="form-grid" onSubmit={(event) => void createProfile(event)}>
          {createError ? (
            <div className="form-span">
              <Alert tone="error" onClose={() => setCreateError(null)}>
                {createError}
              </Alert>
            </div>
          ) : null}
          <Field label={`${t("brandName")} *`} htmlFor="brand-name">
            <input
              id="brand-name"
              dir="ltr"
              value={newName}
              onChange={(event) => setNewName(event.target.value)}
              placeholder="vandefence"
              disabled={creating}
              required
            />
          </Field>
          <Field label={t("brandDisplayName")} htmlFor="brand-display-name">
            <input
              id="brand-display-name"
              dir="auto"
              value={newDisplayName}
              onChange={(event) => setNewDisplayName(event.target.value)}
              placeholder="وندفنس"
              disabled={creating}
            />
          </Field>
          <Field label={t("brandIndustry")} htmlFor="brand-industry">
            <input
              id="brand-industry"
              dir="auto"
              value={newIndustry}
              onChange={(event) => setNewIndustry(event.target.value)}
              disabled={creating}
            />
          </Field>
          <Field label={t("brandDomain")} htmlFor="brand-domain">
            <input
              id="brand-domain"
              dir="ltr"
              value={newDomain}
              onChange={(event) => setNewDomain(event.target.value)}
              placeholder="example.com"
              disabled={creating}
            />
          </Field>
          <Field label={t("brandTone")} htmlFor="brand-tone">
            <input
              id="brand-tone"
              dir="auto"
              value={newTone}
              onChange={(event) => setNewTone(event.target.value)}
              disabled={creating}
            />
          </Field>
          <Field label={t("brandLanguage")} htmlFor="brand-language">
            <input
              id="brand-language"
              value={locale === "fa" ? "fa-IR" : "en-US"}
              readOnly
              dir="ltr"
            />
          </Field>
          <div className="form-span">
            <Field label={t("brandDescription")} htmlFor="brand-description">
              <textarea
                id="brand-description"
                dir="auto"
                rows={3}
                value={newDescription}
                onChange={(event) => setNewDescription(event.target.value)}
                disabled={creating}
              />
            </Field>
          </div>
        </form>
      </Modal>

      <Modal
        open={adminOpen}
        wide
        title={t("adminTitle")}
        subtitle={t("adminSubtitle")}
        onClose={() => setAdminOpen(false)}
        footer={
          <button
            type="button"
            className="button button--ghost"
            onClick={() => void loadPending()}
            disabled={adminLoading}
          >
            {adminLoading ? <Spinner /> : t("refresh")}
          </button>
        }
      >
        {adminError ? (
          <Alert tone="error" onClose={() => setAdminError(null)}>
            {adminError}
          </Alert>
        ) : null}

        {adminLoading ? (
          <Spinner label={t("loading")} />
        ) : adminUsers.length === 0 ? (
          <p className="empty-inline">{t("noPendingUsers")}</p>
        ) : (
          <ul className="admin-list">
            {adminUsers.map((account) => (
              <li key={account.id} className="admin-row">
                <span className="avatar">{initials(account.full_name)}</span>
                <div className="admin-info">
                  <strong>{account.full_name}</strong>
                  <span dir="ltr">{account.email}</span>
                  <small>{formatDateTime(account.created_at)}</small>
                </div>
                <div className="admin-actions">
                  <button
                    type="button"
                    className="button button--success button--tiny"
                    onClick={() => void decide(account.id, true)}
                    disabled={decisionId === account.id}
                  >
                    {decisionId === account.id ? <Spinner /> : t("approve")}
                  </button>
                  <button
                    type="button"
                    className="button button--danger button--tiny"
                    onClick={() => void decide(account.id, false)}
                    disabled={decisionId === account.id}
                  >
                    {t("reject")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
      </Modal>
    </div>
  );
}
