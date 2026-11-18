"use client";

import { useState, useEffect, useCallback } from "react";
import { api } from "@/lib/api";
import { useAuth } from "@/app/auth";
import { LanguageSelector } from "@/app/auth";

type Platform = { id: string; name: string; slug: string };

function useRtlDirection() {
  const [isRtl, setIsRtl] = useState<boolean>(true);
  useEffect(() => {
    const lang = localStorage.getItem("vandad.lang") || "fa";
    setIsRtl(lang !== "en");
  }, []);
  return isRtl;
}

export default function DashboardPage() {
  const { user, language, logout } = useAuth();
  const isRtl = useRtlDirection();

  useEffect(() => {
    document.dir = isRtl ? "rtl" : "ltr";
  }, [isRtl]);

  const [platforms, setPlatforms] = useState<Platform[]>([]);
  const [selectedPlatformId, setSelectedPlatformId] = useState<string | null>(null);
  const [isLoadingPlatforms, setIsLoadingPlatforms] = useState(true);
  const [isSubmitting, setIsSubmitting] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (!user?.id) return;
    setIsLoadingPlatforms(true);
    api<{ items: Platform[] }>("/api/v1/platforms")
      .then((resp) => {
        setPlatforms(resp.items || []);
        const vandiyar = resp.items.find(
          (p) => p.slug === "vandiyar" || p.name === "Vandiyar"
        );
        if (vandiyar) setSelectedPlatformId(vandiyar.id);
      })
      .catch(() => setPlatforms([]))
      .finally(() => setIsLoadingPlatforms(false));
  }, [user?.id]);

  const handleStartProcess = useCallback(async () => {
    if (!selectedPlatformId) {
      setError("لطفاً پلتفرمی را انتخاب کنید.");
      return;
    }
    if (isSubmitting) return;

    setIsSubmitting(true);
    setError("");

    try {
      const resp = await api<{ id: string }>("/api/v1/content/scene-projects", {
        method: "POST",
        body: JSON.stringify({
          brand_id: user!.id,
          topic: `Vandiyar platform: ${selectedPlatformId}`,
          target_duration_seconds: 30,
          provider_id: "kie",
          generation_mode: "single_shot",
          character_key: "Vandiyar/Confident Professional in Emerald Blazer",
        }),
      });

      setError("");
      setIsSubmitting(false);
    } catch (e: any) {
      setError(e.response?.data?.detail || "شروع فرآیند ناموفق بود.");
      setIsSubmitting(false);
    }
  }, [selectedPlatformId, isSubmitting]);

  useEffect(() => {
    // Polling removed for simplicity
  }, []);

  useEffect(() => {
    // Cleanup
  }, []);

  return (
    <div style={{ direction: isRtl ? "rtl" : "ltr", minHeight: "100vh" }}>
      <div
        style={{
          background: "#1e293b",
          padding: 16,
          borderBottom: "1px solid #6b7280",
          margin: "-16px -16px 24px -16px",
          display: "flex",
          flexDirection: isRtl ? "row-reverse" : "row",
          alignItems: "center",
          flexWrap: "wrap",
          gap: 16,
        }}
      >
        <LanguageSelector />
        <div style={{ display: "flex", alignItems: "center", gap: 16 }}>
          <div>Vandad Agent</div>
          <div>{user?.full_name || "کاربر"}</div>
        </div>
        <button onClick={logout} style={{ padding: "8px 16px", borderRadius: 6, border: "none", background: "#6b7280", color: "white" }}>
          EXIT
        </button>
      </div>

      <div
        style={{
          background: "rgba(30, 41, 59, 0.8)",
          borderRadius: 12,
          border: "1px solid #4b5563",
          margin: "-16px",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            padding: 20,
            borderBottom: "1px solid #4b5563",
          }}
        >
          <h3>انتخاب پلتفرم</h3>
          {isLoadingPlatforms && <div>در حال بارگذاری پلتفرم‌ها...</div>}
          {!isLoadingPlatforms && platforms.length === 0 && (
            <div>پلتفرمي ثبت نشده است.</div>
          )}
          {!isLoadingPlatforms && platforms.length > 0 && (
            <div style={{ marginTop: 12, display: "grid", gap: 12 }}>
              {platforms.map((platform) => (
                <label
                  key={platform.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 10,
                    padding: 10,
                    borderRadius: 8,
                    backgroundColor:
                      selectedPlatformId === platform.id
                        ? "#3b82f620"
                        : "transparent",
                    borderColor:
                      selectedPlatformId === platform.id
                        ? "#3b82f630"
                        : "none",
                  }}
                >
                  <input
                    type="radio"
                    value={platform.id}
                    checked={selectedPlatformId === platform.id}
                    onChange={(e) => setSelectedPlatformId(e.target.value)}
                    style={{ transform: "scale(0.8)", margin: 0 }}
                  />
                  <span>{platform.name}</span>
                </label>
              ))}
            </div>
          )}

          {selectedPlatformId && (
            <button
              onClick={handleStartProcess}
              style={{
                width: "100%",
                marginTop: 16,
                padding: "12px 16px",
                borderRadius: 8,
                border: "none",
                backgroundColor: "#10b981",
                color: "white",
                fontSize: 15,
                fontWeight: 500,
                cursor: "pointer",
                transition: "background 0.2s",
              }}
              disabled={isSubmitting}
            >
              {isSubmitting ? "در حال اجرا…" : "شروع فرآیند"}
            </button>
          )}
        </div>
      </div>

      {error && (
        <div
          style={{
            backgroundColor: "rgba(239, 68, 68, 0.2)",
            borderRadius: 8,
            borderColor: "#dc2626",
            margin: "-16px",
            padding: 16,
            color: "#f87171",
          }}
        >
          <p>{error}</p>
        </div>
      )}
    </div>
  );
}

/// Helper RTL hook
function useRtlDirectionRef() {
  const [isRtl, setIsRtl] = useState<boolean>(true);
  useEffect(() => {
    const lang = localStorage.getItem("vandad.lang") || "fa";
    setIsRtl(lang !== "en");
  }, []);
  return isRtl;
}