"use client";

import { useState } from "react";
import { api } from "@/lib/api";

export default function LoginPage() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleLogin = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      const resp = await api<{ user: any; token: string }>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      // Store user and token
      localStorage.setItem("vandad.auth", JSON.stringify(resp.user));
      localStorage.setItem("vandad.token", resp.token);
      // Redirect to dashboard
      window.location.href = "/";
    } catch (e: any) {
      setError(e.response?.data?.detail || "ورود ناموفق بود.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div style={{ direction: "rtl", minHeight: "100vh", background: "#0f172a", color: "#f1f5f9" }}>
      <div style={{ maxWidth: 420, margin: "80px auto", padding: 32, borderRadius: 12, background: "rgba(17, 24, 39, 0.8)", border: "1px solid rgba(100, 115, 147, 0.3)" }}>
        <h2 style={{ textAlign: "right", marginBottom: 24, fontSize: 24 }}>ورود به Vandad Agent</h2>

        {error && (
          <div style={{ background: "#dc2626", color: "white", padding: 12, borderRadius: 8, marginBottom: 16, textAlign: "right" }}>
            {error}
          </div>
        )}

        <form onSubmit={handleLogin} style={{ direction: "rtl" }}>
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
            <label style={{ fontSize: 12, cursor: "pointer" }}>
              %(یادآوری کن من
              <input
                type="checkbox"
                style={{ transform: "scale(0.8)", marginRight: "4px" }}
              />
            </label>
            <button
              type="submit"
              style={{
                flex: 1,
                padding: "10px 16px",
                borderRadius: 6,
                border: "none",
                background: "#3b82f6",
                color: "white",
                fontWeight: 500,
                cursor: "pointer",
              }}
              disabled={loading}
            >
              {loading ? "در حال ورود…" : "ورود"}
            </button>
          </div>

          <div style={{ marginTop: 16, textAlign: "right", fontSize: 13 }}>
            <a href="/register" style={{ color: "#94a3b8", textDecoration: "none" }}>حساب کاربری ندارید؟ ثبت نام کنید</a>
          </div>
        </form>
      </div>
    </div>
  );
}