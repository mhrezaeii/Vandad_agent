"use client";

import { useEffect } from "react";

export default function HomePage() {
  // This page is now protected by the AuthGate in layout.tsx
  // If user is not authenticated, AuthGate will show Login
  // If user is pending_approval, AuthGate will show PendingApproval
  // If user is approved, AuthGate will render the actual dashboard
  
  return null;
}