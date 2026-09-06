/**
 * Representative excerpt from the production frontend (static/app.js),
 * unmodified except for surrounding context. Shows how the client handles
 * the three states the usage gate (backend/usage_gate.py) can put an
 * analysis request in: normal, approaching-the-limit, and blocked.
 *
 * Deliberately quiet by design: the warning is a small, secondary line of
 * text -- never a modal, never something that competes with the actual
 * analysis result for attention.
 */

function renderUsageNote(usage) {
  const el = document.getElementById("usage-note");
  if (!el) return;
  if (!usage || usage.bypassed || !usage.warn) {
    el.classList.add("hidden");
    el.textContent = "";
    return;
  }
  el.textContent = `${usage.remaining} analyses remaining today. RSF limits ` +
    `usage to keep the free version available to everyone.`;
  el.classList.remove("hidden");
}

function showLimitReached(data) {
  document.getElementById("limit-message").textContent = data.message ||
    "You've reached today's analysis limit.";
  const contactP = document.getElementById("limit-contact");
  if (data.contact_email) {
    document.getElementById("limit-contact-link").href =
      `mailto:${data.contact_email}?subject=RSF%20Additional%20Access`;
    contactP.classList.remove("hidden");
  } else {
    contactP.classList.add("hidden");
  }
  document.getElementById("override-error").classList.add("hidden");
  document.getElementById("override-success").classList.add("hidden");
  document.getElementById("limit-reached").classList.remove("hidden");
}

// Redeeming an owner-issued extended-access code. Never reveals whether a
// code was close to correct -- the server returns one generic error either
// way (see backend/usage_gate.py's check_override_code).
document.getElementById("override-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = document.getElementById("override-error");
  const ok = document.getElementById("override-success");
  err.classList.add("hidden");
  ok.classList.add("hidden");
  const code = document.getElementById("override-code").value.trim();
  if (!code) return;
  try {
    const res = await fetch("/api/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ code }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) {
      err.textContent = data.error || "Invalid code.";
      err.classList.remove("hidden");
      return;
    }
    document.getElementById("override-code").value = "";
    ok.classList.remove("hidden");
  } catch (e2) {
    err.textContent = "Could not reach the server. Try again.";
    err.classList.remove("hidden");
  }
});
