"use client";

import { useEffect, useRef } from "react";
import { useSearchParams } from "next/navigation";
import { driver } from "driver.js";
import "driver.js/dist/driver.css";
import { useUser } from "@/components/user-context";
import { api } from "@/lib/api";
import type { User } from "@/lib/types";

/**
 * Post-first-login walkthrough (user-persona-profile spec R2; FSD §4.4.1 Step 3).
 * Auto-starts once after onboarding; replayable via /home?tour=replay.
 */
export default function AppTour() {
  const { user, setUser } = useUser();
  const searchParams = useSearchParams();
  const startedRef = useRef(false);

  const replay = searchParams.get("tour") === "replay";

  useEffect(() => {
    if (!user || startedRef.current) return;
    if (!replay && user.tour_completed) return;
    if (!user.onboarding_completed) return;
    startedRef.current = true;

    const business = user.persona !== "power";

    const persistDone = async () => {
      if (user.tour_completed) return;
      try {
        const result = await api<{ user: User }>("/v1/users/me", {
          method: "PUT",
          body: JSON.stringify({ tour_completed: true }),
        });
        setUser(result.user);
      } catch {
        /* non-blocking */
      }
    };

    const tour = driver({
      showProgress: true,
      overlayOpacity: 0.65,
      popoverClass: "marshal-tour",
      onDestroyed: () => void persistDone(),
      steps: [
        {
          popover: {
            title: "Welcome to marshal 🏭",
            description:
              "A 30-second tour of the main features. You can replay it anytime from your profile.",
          },
        },
        {
          element: '[data-tour="chat"]',
          popover: {
            title: business ? "Build your agent" : "AI Chat",
            description: business
              ? "Describe your idea in plain English. marshal asks clarifying questions and shapes the plan with you."
              : "Freeform chat with the model. Describe requirements, iterate, refine.",
          },
        },
        {
          element: '[data-tour="generate"]',
          popover: {
            title: "Generate requirements",
            description:
              "When the conversation has enough detail, one click produces a versioned, structured requirements.md you can preview and download.",
          },
        },
        {
          element: '[data-tour="projects"]',
          popover: {
            title: "My Projects",
            description:
              "Every generated spec lives in a project. Track status from draft to spec-complete to deployed.",
          },
        },
        {
          element: '[data-tour="deploy-card"]',
          popover: {
            title: "Deploy to an Enclave",
            description:
              "Deploy a working app into an isolated, governed AWS Enclave account, watch live status, and tear it down when done.",
          },
        },
        {
          element: '[data-tour="profile"]',
          popover: {
            title: "Profile & settings",
            description:
              "Edit your details, manage the profile options available to your account, and replay this tour.",
          },
        },
      ],
    });
    tour.drive();
    return () => tour.destroy();
  }, [user, replay, setUser]);

  return null;
}
