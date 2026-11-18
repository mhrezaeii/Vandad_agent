"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, apiBlob, errorMessage } from "@/lib/api";
import type {
  ContentProject,
  WorkflowExecution,
  WorkflowStep,
} from "@/lib/types";

const ACTIVE_KEY = "vandad.activeProject";
const POLL_INTERVAL_MS = 3000;

export const ACTIVE_PROJECT_STATUSES = new Set<string>([
  "draft",
  "script_generating",
  "script_ready",
  "approved",
  "scenes_prepared",
  "video_generation_pending",
  "video_generating",
  "video_generated",
  "assembling",
]);

const VIDEO_STAGE_STATUSES = new Set<string>([
  "approved",
  "scenes_prepared",
  "video_generation_pending",
  "video_generating",
  "video_generated",
  "assembling",
]);

export type StartInput = {
  brandId: string;
  topic: string;
  durationSeconds: number;
  providerId: string | null;
  characterKey: string | null;
};

export type ExecutionKind = "script" | "video";

type WorkflowStepList = { items: WorkflowStep[]; total: number };

export function useProduction() {
  const [project, setProject] = useState<ContentProject | null>(null);
  const [execution, setExecution] = useState<WorkflowExecution | null>(null);
  const [steps, setSteps] = useState<WorkflowStep[]>([]);
  const [executionKind, setExecutionKind] = useState<ExecutionKind>("script");
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [restoring, setRestoring] = useState(true);

  const projectRef = useRef<ContentProject | null>(null);
  const kindRef = useRef<ExecutionKind>("script");
  const startLock = useRef(false);
  const advanceLock = useRef(false);
  const advanceAttempts = useRef<Record<string, number>>({});
  const videoRef = useRef<string | null>(null);
  const videoLoadingRef = useRef<string | null>(null);

  const setProjectState = useCallback((next: ContentProject | null) => {
    projectRef.current = next;
    setProject(next);
  }, []);

  const clearVideo = useCallback(() => {
    if (videoRef.current) URL.revokeObjectURL(videoRef.current);
    videoRef.current = null;
    videoLoadingRef.current = null;
    setVideoUrl(null);
  }, []);

  const loadVideo = useCallback(
    async (current: ContentProject) => {
      if (
        current.status !== "completed" ||
        !current.final_artifact_id ||
        videoRef.current ||
        videoLoadingRef.current === current.id
      ) {
        return;
      }
      videoLoadingRef.current = current.id;
      try {
        const blob = await apiBlob(`/content/videos/${current.id}/content`);
        if (videoRef.current) URL.revokeObjectURL(videoRef.current);
        videoRef.current = URL.createObjectURL(blob);
        setVideoUrl(videoRef.current);
      } finally {
        videoLoadingRef.current = null;
      }
    },
    [],
  );

  const syncExecution = useCallback(
    async (current: ContentProject, kind: ExecutionKind) => {
      const executionId =
        kind === "video"
          ? current.video_workflow_execution_id
          : current.script_workflow_execution_id;
      if (!executionId) {
        setExecution(null);
        setSteps([]);
        return;
      }
      const [nextExecution, stepList] = await Promise.all([
        api<WorkflowExecution>(`/executions/${executionId}`),
        api<WorkflowStepList>(`/executions/${executionId}/steps`),
      ]);
      setExecution(nextExecution);
      setSteps(stepList.items);
    },
    [],
  );

  const syncProject = useCallback(
    async (projectId: string) => {
      const next = await api<ContentProject>(`/content/projects/${projectId}`);
      setProjectState(next);
      await syncExecution(next, kindRef.current);
      await loadVideo(next);
      return next;
    },
    [loadVideo, setProjectState, syncExecution],
  );

  const refresh = useCallback(async () => {
    const current = projectRef.current;
    if (!current) return null;
    try {
      return await syncProject(current.id);
    } catch (cause) {
      setError(errorMessage(cause));
      return null;
    }
  }, [syncProject]);

  const selectExecution = useCallback(
    (kind: ExecutionKind) => {
      kindRef.current = kind;
      setExecutionKind(kind);
      const current = projectRef.current;
      if (current) {
        void syncExecution(current, kind).catch((cause) =>
          setError(errorMessage(cause)),
        );
      }
    },
    [syncExecution],
  );

  // Restore an in-flight project after a page reload.
  useEffect(() => {
    let cancelled = false;
    const saved =
      typeof window !== "undefined"
        ? window.localStorage.getItem(ACTIVE_KEY)
        : null;
    if (!saved) {
      setRestoring(false);
      return;
    }
    (async () => {
      try {
        const next = await api<ContentProject>(`/content/projects/${saved}`);
        if (cancelled) return;
        setProjectState(next);
        const kind: ExecutionKind = next.video_workflow_execution_id
          ? "video"
          : "script";
        kindRef.current = kind;
        setExecutionKind(kind);
        await syncExecution(next, kind);
        await loadVideo(next);
      } catch (cause) {
        if (cancelled) return;
        const status = (cause as { status?: number }).status;
        if (status === 404 || status === 403) {
          window.localStorage.removeItem(ACTIVE_KEY);
        } else {
          setError(errorMessage(cause));
        }
      } finally {
        if (!cancelled) setRestoring(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [loadVideo, setProjectState, syncExecution]);

  // Poll while the pipeline is in flight.
  useEffect(() => {
    if (!project || !ACTIVE_PROJECT_STATUSES.has(project.status)) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [project?.id, project?.status, refresh, project]);

  // Advance the real backend pipeline through its operator gates. This never
  // fabricates progress: each call starts an actual backend workflow.
  useEffect(() => {
    if (!project) return;
    if (project.status !== "script_ready" && project.status !== "approved") {
      return;
    }
    if (advanceLock.current) return;
    const stageKey = `${project.id}:${project.status}`;
    const attempts = advanceAttempts.current[stageKey] ?? 0;
    // A permanent backend rejection must not be retried forever on every poll.
    if (attempts >= 3) return;
    advanceAttempts.current[stageKey] = attempts + 1;
    const current = project;
    advanceLock.current = true;
    (async () => {
      try {
        if (current.status === "script_ready") {
          await api(`/content/scene-projects/${current.id}/approve-scenario`, {
            method: "POST",
            body: JSON.stringify({ approved: true }),
          });
        }
        await api(`/content/scene-projects/${current.id}/generate-video`, {
          method: "POST",
          body: JSON.stringify({
            provider_id: current.scene_video_provider ?? undefined,
          }),
        });
        const next = await syncProject(current.id);
        if (next.video_workflow_execution_id) {
          kindRef.current = "video";
          setExecutionKind("video");
          await syncExecution(next, "video");
        }
      } catch (cause) {
        setError(errorMessage(cause));
      } finally {
        advanceLock.current = false;
      }
    })();
  }, [project, syncExecution, syncProject]);

  useEffect(() => {
    return () => {
      if (videoRef.current) URL.revokeObjectURL(videoRef.current);
    };
  }, []);

  const start = useCallback(
    async (input: StartInput) => {
      if (startLock.current) {
        throw new Error("duplicate-submission");
      }
      startLock.current = true;
      setBusy(true);
      setError(null);
      clearVideo();
      try {
        const created = await api<ContentProject>("/content/scene-projects", {
          method: "POST",
          body: JSON.stringify({
            brand_id: input.brandId,
            topic: input.topic,
            target_duration_seconds: input.durationSeconds,
            provider_id: input.providerId ?? undefined,
            character_key: input.characterKey ?? undefined,
          }),
        });
        window.localStorage.setItem(ACTIVE_KEY, created.id);
        setProjectState(created);
        const started = await api<ContentProject>(
          `/content/scene-projects/${created.id}/generate-scenario`,
          { method: "POST", body: JSON.stringify({}) },
        );
        setProjectState(started);
        kindRef.current = "script";
        setExecutionKind("script");
        await syncExecution(started, "script");
        return started;
      } finally {
        startLock.current = false;
        setBusy(false);
      }
    },
    [clearVideo, setProjectState, syncExecution],
  );

  const cancel = useCallback(async () => {
    const current = projectRef.current;
    if (!current || cancelling) return;
    setCancelling(true);
    setError(null);
    try {
      const next = await api<ContentProject>(
        `/content/scene-projects/${current.id}/cancel`,
        { method: "POST" },
      );
      setProjectState(next);
      await syncExecution(next, kindRef.current);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setCancelling(false);
    }
  }, [cancelling, setProjectState, syncExecution]);

  const retryScenario = useCallback(async () => {
    const current = projectRef.current;
    if (!current || busy) return;
    setBusy(true);
    setError(null);
    try {
      const started = await api<ContentProject>(
        `/content/scene-projects/${current.id}/generate-scenario`,
        { method: "POST", body: JSON.stringify({}) },
      );
      setProjectState(started);
      await syncExecution(started, "script");
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setBusy(false);
    }
  }, [busy, setProjectState, syncExecution]);

  const reset = useCallback(() => {
    window.localStorage.removeItem(ACTIVE_KEY);
    clearVideo();
    setProjectState(null);
    setExecution(null);
    setSteps([]);
    setError(null);
    kindRef.current = "script";
    setExecutionKind("script");
  }, [clearVideo, setProjectState]);

  return {
    project,
    execution,
    steps,
    executionKind,
    videoUrl,
    error,
    busy,
    cancelling,
    restoring,
    start,
    cancel,
    retryScenario,
    reset,
    refresh,
    selectExecution,
    setError,
    isVideoStage: project ? VIDEO_STAGE_STATUSES.has(project.status) : false,
  };
}
