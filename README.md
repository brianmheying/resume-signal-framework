# Resume Signal Framework (RSF)

**Live product:** https://resumesignalframework.com

RSF answers one question before you spend an evening tailoring a resume:
**is this job actually worth pursuing?**

This repository is a curated proof-of-work artifact for RSF, not the
complete production source. It shows the real architecture, real
engineering decisions, and real code for the parts that are safe to share
publicly — the evaluation methodology itself is intentionally excluded.
See [What's not included](#whats-not-included-and-why) for exactly why.

## The problem

Most job-search tooling either inflates a resume-match percentage or
auto-applies to as many postings as possible. Neither answers the actual
question a candidate has: *given who I am, is this specific role worth the
hours it takes to tailor an application?* A 94% keyword match doesn't tell
you if you can do the job, and volume-based tooling just spreads a weak fit
across more postings, faster.

## Why I built it

I kept doing this evaluation manually, ad hoc, for my own job search —
reading a JD, weighing it against my actual background and what I was
trying to move toward, and deciding whether it was worth the time. RSF is
that judgment turned into a repeatable framework, then into working
software.

## What a user does

1. **Build a candidate profile once** — title/level, experience, functional
   expertise, comp target, career goals, and similar. This lives only in
   the browser (`localStorage`), never on the server.
2. **Paste a job description.** A resume is optional and only adds detail;
   the profile is the primary scoring input.
3. Get back a structured verdict, not a wall of text.

## What comes back

Four independent measures, on purpose kept from contaminating one another:

| Measure | Question it answers |
|---|---|
| **Truth Score** (0–100) | Can I actually do this job? Pure capability — never adjusted for comp, location, or preference. |
| **Bucket** | How would an employer perceive this candidate in a fast resume scan? Market perception, independent of whether the candidate wants the role. |
| **Strategic Adjustment** (a bounded adjustment in either direction) | Given my own career strategy and preferences, how much should this pull for or against me, on top of raw capability? |
| **Desire Score / Grade** | Truth Score + Strategic Adjustment, computed deterministically server-side — never by the model — and turned into a letter grade. |

A role can score high on capability and still be a bad strategic bet. RSF
says so explicitly instead of collapsing everything into one number.

## How it works

See **[docs/architecture.md](docs/architecture.md)** for the full request-flow
diagram. In short: a stdlib-only Python backend on Render, a static
HTML/CSS/JS frontend, one structured-output call to Claude per analysis, and
a Google Sheet as the entire "database" via a small Apps Script webhook.

## What I built

- **The evaluation framework itself** — the four-measure model above, and
  the rules for what belongs in which layer (see the framework, not the
  literal prompt — [scope note](#whats-not-included-and-why)).
- **[`backend/usage_gate.py`](backend/usage_gate.py)** — a per-IP daily rate
  limiter protecting a paid, abusable API surface: keyed IP hashing (never
  a raw IP stored), an atomic counter, an owner-issued override-code
  mechanism, and a durable backend client that **fails open** rather than
  taking the product down when its dependency misbehaves.
- **[`apps_script/ledger_apps_script.gs`](apps_script/ledger_apps_script.gs)**
  — the actual Apps Script integration, doing double duty as a usage ledger
  and the rate limiter's durable counter, with `LockService` locking so
  concurrent requests can't race past the limit.
- **[`backend/structured_output_example.py`](backend/structured_output_example.py)**
  — the real pattern (simplified prompt/schema) for enforcing a reliable
  JSON shape from Claude, and computing the final grade deterministically
  server-side rather than trusting the model to do arithmetic.
- **[`frontend/usage_gate_ui.js`](frontend/usage_gate_ui.js)** — the client
  side of the rate limiter: a quiet warning as the limit approaches, and a
  friendly blocked-state with a way to request extended access.
- **A real test suite** — 64 tests in production; this repo includes a
  runnable representative subset (see [Testing & Production Hardening](#testing--production-hardening)).

## Key decisions

- **Four independent measures instead of one blended score.** Capability,
  market perception, and personal strategy answer different questions and
  were originally conflated into a single "score" — an earlier version of
  RSF did exactly that, and it produced advice that was directionally
  right but couldn't explain *why*. Splitting them was the single biggest
  architecture change RSF went through.
- **The model never computes the final grade.** It outputs `truth_score`
  and `strategic_adjustment` as independent numbers; the server adds them
  and derives the letter grade. This keeps the arithmetic always correct
  and auditable, and it's a deliberate boundary: the LLM reasons about
  fit, the server owns the math.
- **Zero server-side persistence.** No database, no per-user history. The
  candidate profile lives only in the browser. This wasn't the original
  design — an early version kept server-side history, and it was removed
  once it became clear that was an unnecessary cross-user data-visibility
  risk with no real benefit.
- **Rate limiting fails open, not closed.** If the durable counter's
  backend is unreachable, requests are still allowed through, uncounted,
  rather than the product becoming unusable because of an unrelated
  dependency. The trade-off is explicit: bounded, temporary under-counting
  is preferable to real users getting a broken product.
- **Zero-dependency backend.** Python's standard library only — no
  framework, no build step. A deliberate simplicity choice for a
  single-purpose tool, not a scalability stance.

## Where the LLM is bounded vs. where logic is deterministic

The model is asked to *reason* about capability fit, market perception, and
strategic pull — it is never asked to do arithmetic, decide the final
letter grade, or enforce the daily usage limit. Those are all plain
deterministic code: a fixed formula for the grade, a counter with a fixed
threshold for the rate limit. The LLM is one component with a narrow,
well-defined job inside a system that has a deterministic boundary around
it — not the whole system.

## Usage-gating / API-cost protection

Every request passes a rate-limit check *before* the paid Anthropic call,
not after — an over-limit request never reaches the model. The check
itself only ever transmits a keyed hash of the visitor's IP, never the
IP itself. A person who needs more access can request an owner-issued
override code, checked in constant time against a small configured list,
with a deliberately generic error on failure so it never reveals whether a
given code was close to correct. Full detail in
[`backend/usage_gate.py`](backend/usage_gate.py).

## Testing & Production Hardening

Production runs a 64-test suite across three layers: pure logic (rolling
time windows, warning thresholds), full HTTP integration against a mock
Anthropic mode, and a local double standing in for the real Google Apps
Script webhook (so paid-API and third-party-service failure modes can be
tested without hitting either).

This repo includes a self-contained, runnable subset of that suite:

```bash
cd tests
python3 test_usage_gate.py
```

Expected output: `11 passed, 0 failed`. No network access, no dependencies,
no Anthropic key required.

RSF has one real production incident worth describing honestly, because
the fix mattered more than the bug.

The usage ledger (a Google Apps Script webhook) started silently failing
to write rows. Every analysis kept completing normally for users — nothing
looked broken. The root cause: the Apps Script backend was returning a
normal HTTP 200 with an application-level `{"ok": false, "error": "..."}`
body, and the client code that posted to it only checked whether the HTTP
request itself succeeded — it never inspected the response body. A
rejected write and a successful write looked identical from the outside.

Diagnosing it meant ruling out token mismatches, stale deployment versions,
and JSON-escaping issues one at a time, using a local mock of the Apps
Script webhook to reproduce each failure mode without touching production
or spending on real API calls. Once isolated, the fix was narrow: parse
the response, treat `ok: false` as a real failure, and log it clearly —
while explicitly preserving the ledger's non-blocking design, so a logging
failure still can never turn into a failure the user experiences.

The lesson wasn't "test more" in the abstract — it was that **a dependency
returning "200 OK" is not the same claim as a dependency saying "I actually
did the thing you asked."** Both the ledger client and the rate limiter's
durable backend now check that distinction explicitly.

## Privacy / data handling

- No candidate profile, resume, or job description is ever stored
  server-side — the profile lives only in the browser.
- The only thing written to a persistent store is non-sensitive usage
  metadata (company, role, score, grade) for the operator's own product
  analytics — never resume or JD content.
- IP addresses are never stored raw; only a one-way keyed hash is ever
  transmitted to the rate limiter's backend.

## What's not included and why

This repository is evidence of the build, not the complete production
source. Specifically excluded:

- **The production system prompt and its exact scoring rubric** — the
  literal instructions that tell the model how to weigh evidence within
  each measure. This is the actual proprietary methodology behind RSF;
  publishing it would let anyone reproduce the evaluation logic wholesale.
  [`backend/structured_output_example.py`](backend/structured_output_example.py)
  shows the *pattern* used, with a deliberately simplified stand-in prompt
  and schema.
- **The full production request handler** (`server.py`) — the pieces of it
  that are safe and useful to show in isolation (the rate limiter, the
  structured-output pattern) are included; the complete file, including
  routing, auth, and file-upload handling, is not.
- **Real production configuration** — the live Sheet ID, webhook URL, API
  keys, and access codes. Every config example in this repo
  (`deploy/.env.example`) uses placeholders.
- **Internal operational notes and beta-tester data** — none of which
  belongs in a public repository regardless of how it's built.

## Try it

**https://resumesignalframework.com**

## What this demonstrates

Identifying a real, repeated problem; designing a decision framework to
solve it, not just a UI; integrating an LLM as one bounded component
inside a system with deterministic guarantees around it; building real
controls around a paid, abusable API surface; instrumenting and testing
real failure modes (including a third-party service that fails silently
rather than loudly); and operating the result as a live product,
end to end.

## Contact

Brian Heying — [brianheying.com](https://www.brianheying.com)

## License

MIT — see [LICENSE](LICENSE). The license covers only the code and
documentation in this repository. It does not extend to the production
evaluation prompt/methodology or the "Resume Signal Framework" / "RSF"
name and branding, neither of which is included here.
