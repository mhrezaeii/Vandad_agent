"use client";

import { useState } from "react";
import { api } from "@/lib/api";

export default function RegisterPage() {
  const [fullName, setFullName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    
    if (password !== confirmPassword) {
      setError("رمزعبورهای یکسان نیستند.");
      setLoading(false);
      return;
    }
    
    try {
      const resp = await api<{ user: any }>("/auth/register", {
        method: "POST",
        body: JSON.stringify({ full_name: fullName, email, password }),
      });
      // New accounts start as pending_approval
      localStorage.setItem("vandad.auth", JSON.stringify(resp.user));
      // Redirect to pending approval page
      window.location.href = "/pending-approval";
    } catch (e: any) {
      setError(e.response?.data?.detail || "ثبت نام ناموفق بود.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ direction: "rtl", minHeight: "100vh", background: "#0f172a", color: "#f1f5f9" }}>
      <div style={{ maxWidth: 460, margin: "80px auto", padding: 32, borderRadius: 12, background: "rgba(17, 24, 39, 0.8)", border: "1px solid rgba(100, 115, 147, 0.3)" }}>
        <h2 style={{ textAlign: "right", marginBottom: 24, fontSize: 24 }}>ثبت نام جدید</h2>

        {error && (
          <div style={{ background: "#dc2626", color: "white", padding: 12, borderRadius: 8, marginBottom: 16, textAlign: "right" }}>
            {error}
          </div>
        )}

        <form onSubmit={handleRegister} style={{ direction: "rtl" }}>
          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6 }}>نام کامل</label>
            <input
              type="text"
              value={fullName}
              onChange={(e) => setFullName(e.target.value)}
              required
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 6,
                border: "1px solid rgba(100, 115, 147, 0.5)",
                background: "rgba(30, 41, 59, 0.5)",
                color: "#f1f5f9",
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6 }}>ایمیل</label>
            <input
              type="email"
              value={email}
              onChange={(e) => setEmail(e.target.value)}
              required
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 6,
                border: "1px solid rgba(100, 115, 147, 0.5)",
                background: "rgba(30, 41, 59, 0.5)",
                color: "#f1f5f9",
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6 }}>رمزعبور</label>
            <input
              type="password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              required
              minLength={8}
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 6,
                border: "1px solid rgba(100, 115, 147, 0.5)",
                background: "rgba(30, 41, 59, 0.5)",
                color: "#f1f5f9",
              }}
            />
          </div>

          <div style={{ marginBottom: 16 }}>
            <label style={{ display: "block", marginBottom: 6 }}>تکرار رمزعبور</label>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              required
              minLength={8}
              style={{
                width: "100%",
                padding: 10,
                borderRadius: 6,
                border: "1px solid rgba(100, 115, 147, 0.5)",
                background: "rgba(30, 41, 59, 0.5)",
                color: "#f1f5f9",
              }}
            />
          </div>

          <div style={{ marginBottom: 20, display: "flex", justifyContent: "flex-end", gap: 8 }}>
            <button
              type="submit"
              style={{
                padding: "10px 20px",
                borderRadius: 6,
                border: "none",
                background: "#10b981",
                color: "white",
                fontFamily: "iransans, system-ui, sans-serif",
                fontWeight: 500,
                cursor: "pointer",
              }}
              disabled={loading}
            >
              {loading ? "در حال ثبت‌نام…" : "ثبت نام"}
            </button>
          </div>

          <div style={{ marginTop: 16, textAlign: "right", fontSize: 13, color: "#6b7280" }}>
            حساب کاربری شما پس از ثبت‌نام وضعیت <span style={{ color: "#f87171", fontWeight: 500 }}>pending_approval</span> خواهد داشت. مدیر سیستم aprobación حساب را پس از بررسی، فعال خواهد کرد.
          </div>
        </form>
      </div>
    </div>
  );
}