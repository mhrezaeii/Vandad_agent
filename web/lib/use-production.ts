"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, apiBlob, errorMessage } from "@/lib/api";
import type {
  AgentTask,
  AgentTaskEvent,
  AgentTaskList,
  ContentProject,
  WorkflowExecution,
  WorkflowStep,
  WorkflowStepList,
} from "@/lib/types";

const ACTIVE_KEY = "vandad.activeTask";
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

export const ACTIVE_TASK_STATUSES = new Set<string>([
  "pending",
  "queued",
  "awaiting_approval",
  "running",
]);

const TERMINAL_TASK_STATUSES = new Set<string>([
  "completed",
  "failed",
  "cancelled",
]);

export type StartInput = {
  brandId: string;
  title: string;
  topic: string | null;
  sourceUrl: string | null;
  durationSeconds: number;
  providerId: string | null;
  characterKey: string | null;
  characterImageUrl?: string | null;
};

/** One backend-reported stage of the content production agent. */
export type StageEntry = {
  id: string;
  sequence: number;
  stage: string;
  status: string | null;
  payload: Record<string, unknown>;
  createdAt: string;
};

function projectIdFrom(
  task: AgentTask | null,
  events: AgentTaskEvent[],
): string | null {
  const fromResult = task?.result_payload?.project_id;
  if (typeof fromResult === "string" && fromResult) return fromResult;
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const value = events[index]?.payload?.project_id;
    if (typeof value === "string" && value) return value;
  }
  return null;
}

function toStages(events: AgentTaskEvent[]): StageEntry[] {
  return events
    .filter(
      (event) => event.event_type === "progress" || event.event_type === "run.failed",
    )
    .map((event) => ({
      id: event.id,
      sequence: event.sequence,
      stage:
        typeof event.payload?.stage === "string"
          ? event.payload.stage
          : event.event_type === "run.failed"
            ? "run.failed"
            : "progress",
      status:
        typeof event.payload?.stage_status === "string"
          ? event.payload.stage_status
          : event.event_type === "run.failed"
            ? "failed"
            : null,
      payload: event.payload,
      createdAt: event.created_at,
    }))
    .sort((left, right) => left.sequence - right.sequence);
}

export function useProduction() {
  const [task, setTask] = useState<AgentTask | null>(null);
  const [events, setEvents] = useState<AgentTaskEvent[]>([]);
  const [project, setProject] = useState<ContentProject | null>(null);
  const [execution, setExecution] = useState<WorkflowExecution | null>(null);
  const [steps, setSteps] = useState<WorkflowStep[]>([]);
  const [history, setHistory] = useState<AgentTask[]>([]);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [videoUrl, setVideoUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [restoring, setRestoring] = useState(true);

  const taskRef = useRef<AgentTask | null>(null);
  const projectRef = useRef<ContentProject | null>(null);
  const startLock = useRef(false);
  const syncLock = useRef(false);
  const videoRef = useRef<string | null>(null);
  const videoProjectRef = useRef<string | null>(null);
  const videoLoadingRef = useRef<string | null>(null);
  const lastTerminalRef = useRef<string | null>(null);

  const setTaskState = useCallback((next: AgentTask | null) => {
    taskRef.current = next;
    setTask(next);
  }, []);

  const setProjectState = useCallback((next: ContentProject | null) => {
    projectRef.current = next;
    setProject(next);
  }, []);

  const clearVideo = useCallback(() => {
    if (videoRef.current) URL.revokeObjectURL(videoRef.current);
    videoRef.current = null;
    videoProjectRef.current = null;
    videoLoadingRef.current = null;
    setVideoUrl(null);
  }, []);

  const loadVideo = useCallback(
    async (current: ContentProject) => {
      if (current.status !== "completed" || !current.final_artifact_id) {
        return;
      }
      if (
        (videoProjectRef.current === current.id && videoRef.current) ||
        videoLoadingRef.current === current.id
      ) {
        return;
      }
      if (videoProjectRef.current !== current.id) clearVideo();
      videoLoadingRef.current = current.id;
      try {
        const blob = await apiBlob(`/content/videos/${current.id}/content`);
        if (videoRef.current) URL.revokeObjectURL(videoRef.current);
        videoRef.current = URL.createObjectURL(blob);
        videoProjectRef.current = current.id;
        setVideoUrl(videoRef.current);
      } finally {
        videoLoadingRef.current = null;
      }
    },
    [clearVideo],
  );

  const syncExecution = useCallback(async (current: ContentProject) => {
    const executionId = current.video_workflow_execution_id;
    if (!executionId) {
      setExecution(null);
      setSteps([]);
      return;
    }
    try {
      const [nextExecution, stepList] = await Promise.all([
        api<WorkflowExecution>(`/executions/${executionId}`),
        api<WorkflowStepList>(`/executions/${executionId}/steps`),
      ]);
      setExecution(nextExecution);
      setSteps(stepList.items);
    } catch (cause) {
      // The execution may still be committing on the backend; the next poll
      // retries. Never surface a transient 404 as a production failure.
      if ((cause as { status?: number }).status !== 404) throw cause;
    }
  }, []);

  const syncProject = useCallback(
    async (projectId: string) => {
      if (projectRef.current?.id !== projectId) clearVideo();
      const next = await api<ContentProject>(`/content/projects/${projectId}`);
      setProjectState(next);
      await syncExecution(next);
      await loadVideo(next);
      return next;
    },
    [clearVideo, loadVideo, setProjectState, syncExecution],
  );

  const syncTask = useCallback(
    async (taskId: string) => {
      const next = await api<AgentTask>(`/agent-tasks/${taskId}`);
      setTaskState(next);
      const eventList = await api<AgentTaskEvent[]>(
        `/agent-tasks/${taskId}/events?after=0&limit=500`,
      );
      setEvents(eventList);
      const projectId = projectIdFrom(next, eventList);
      if (projectId) {
        await syncProject(projectId);
      } else {
        setProjectState(null);
        setExecution(null);
        setSteps([]);
      }
      return next;
    },
    [setProjectState, setTaskState, syncProject],
  );

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const list = await api<AgentTaskList>(
        "/agent-tasks?agent_key=content&limit=20",
      );
      setHistory(
        list.items.filter(
          (item) => item.capability === "content.production.run",
        ),
      );
    } catch {
      // History is supplementary; a failure must not break the live view.
    } finally {
      setHistoryLoading(false);
    }
  }, []);

  const refresh = useCallback(async () => {
    const current = taskRef.current;
    if (!current) {
      await loadHistory();
      return null;
    }
    if (syncLock.current) return current;
    syncLock.current = true;
    try {
      return await syncTask(current.id);
    } catch (cause) {
      setError(errorMessage(cause));
      return null;
    } finally {
      syncLock.current = false;
    }
  }, [loadHistory, syncTask]);

  // Restore the last task after a reload.
  useEffect(() => {
    let cancelled = false;
    const saved =
      typeof window !== "undefined"
        ? window.localStorage.getItem(ACTIVE_KEY)
        : null;
    (async () => {
      try {
        if (saved) {
          await syncTask(saved);
        }
        await loadHistory();
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
  }, [loadHistory, syncTask]);

  // Poll while either the agent task or the video project is still moving.
  const taskActive = !!task && ACTIVE_TASK_STATUSES.has(task.status);
  const projectActive = !!project && ACTIVE_PROJECT_STATUSES.has(project.status);
  useEffect(() => {
    if (!taskActive && !projectActive) return;
    const timer = window.setInterval(() => {
      void refresh();
    }, POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [taskActive, projectActive, refresh]);

  // Refresh history once when the task reaches a terminal state.
  useEffect(() => {
    if (!task) return;
    if (!TERMINAL_TASK_STATUSES.has(task.status)) return;
    if (lastTerminalRef.current === task.id) return;
    lastTerminalRef.current = task.id;
    void loadHistory();
  }, [task, loadHistory]);

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
        const inputPayload: Record<string, unknown> = {
          brand_id: input.brandId,
          target_duration_seconds: input.durationSeconds,
        };
        if (input.topic?.trim()) inputPayload.topic = input.topic.trim();
        if (input.sourceUrl?.trim()) inputPayload.source_url = input.sourceUrl.trim();
        if (input.providerId) inputPayload.provider_id = input.providerId;
        if (input.characterKey) inputPayload.character_key = input.characterKey;
        if (input.characterImageUrl) {
          inputPayload.character_image_url = input.characterImageUrl;
        }
        const created = await api<AgentTask>("/agent-tasks", {
          method: "POST",
          body: JSON.stringify({
            agent_key: "content",
            capability: "content.production.run",
            title: input.title,
            input_payload: inputPayload,
            idempotency_key: `content-production:${input.brandId}:${Date.now()}:${Math.random()
              .toString(36)
              .slice(2, 10)}`,
          }),
        });
        window.localStorage.setItem(ACTIVE_KEY, created.id);
        setTaskState(created);
        setEvents([]);
        setProjectState(null);
        setExecution(null);
        setSteps([]);
        lastTerminalRef.current = null;
        await syncTask(created.id);
        await loadHistory();
        return created;
      } finally {
        startLock.current = false;
        setBusy(false);
      }
    },
    [clearVideo, loadHistory, setProjectState, setTaskState, syncTask],
  );

  const cancel = useCallback(async () => {
    const current = taskRef.current;
    if (!current || cancelling) return;
    setCancelling(true);
    setError(null);
    try {
      await api<AgentTask>(`/agent-tasks/${current.id}/cancel`, {
        method: "POST",
      });
      await syncTask(current.id);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setCancelling(false);
    }
  }, [cancelling, syncTask]);

  const retry = useCallback(async () => {
    const current = taskRef.current;
    if (!current || retrying) return;
    setRetrying(true);
    setError(null);
    try {
      await api<AgentTask>(`/agent-tasks/${current.id}/retry`, {
        method: "POST",
      });
      lastTerminalRef.current = null;
      await syncTask(current.id);
    } catch (cause) {
      setError(errorMessage(cause));
    } finally {
      setRetrying(false);
    }
  }, [retrying, syncTask]);

  const openTask = useCallback(
    async (taskId: string) => {
      setError(null);
      clearVideo();
      setEvents([]);
      setProjectState(null);
      setExecution(null);
      setSteps([]);
      window.localStorage.setItem(ACTIVE_KEY, taskId);
      try {
        await syncTask(taskId);
      } catch (cause) {
        setError(errorMessage(cause));
      }
    },
    [clearVideo, setProjectState, syncTask],
  );

  const reset = useCallback(() => {
    window.localStorage.removeItem(ACTIVE_KEY);
    clearVideo();
    setTaskState(null);
    setEvents([]);
    setProjectState(null);
    setExecution(null);
    setSteps([]);
    setError(null);
    lastTerminalRef.current = null;
  }, [clearVideo, setProjectState, setTaskState]);

  return {
    task,
    events,
    stages: toStages(events),
    project,
    execution,
    steps,
    history,
    historyLoading,
    videoUrl,
    error,
    busy,
    cancelling,
    retrying,
    restoring,
    start,
    cancel,
    retry,
    reset,
    refresh,
    openTask,
    setError,
    isVideoStage: project ? project.status !== "completed" : false,
  };
}
