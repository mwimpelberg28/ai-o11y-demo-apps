// supportbot-anomalies.js
//
// "Problem employee" loadgen. Once every 10 minutes, picks one problem
// from PROBLEMS[] and runs it against one of 5 fixed acme.com users.
// All requests are pinned to Ollama (preferred_provider) so spikes don't
// burn the Claude cap. The first iteration fires immediately on startup
// so you can demo it without waiting 10 minutes.
//
// Adding a new problem type:
//   1. Write a `runXxx(user)` function below — should run for at most a
//      couple of minutes so the next iteration starts on schedule.
//   2. Append `{ id, run }` to PROBLEMS[].
//
// The orchestrator hands this script the first 5 SB users via USERS_FILE,
// so the same 5 acme.com employees rotate as the "repeat offenders" —
// stable across pod restarts as long as users.yaml is unchanged.

import { sleep } from 'k6';
import {
  request, loadUsers, randInt, pickOne, randConversationId,
} from './_common.js';

// ── init context ──────────────────────────────────────────────────────────────

const USERS = loadUsers();                  // already sliced to 5 by orchestrator
const BASE = __ENV.SB_BASE_URL || 'http://sb-web.support-bot.svc.cluster.local';
// Target ~2 bursts per hour, randomly spaced. Each iteration sleeps a
// random 20-40 min after finishing its burst, so on average ~30 min
// elapses between successive starts (= 2/hr) with enough spread that
// audiences don't see a metronome cadence on the dashboards.
const INTERVAL_MIN_SEC = 1200;              // 20 min lower bound
const INTERVAL_MAX_SEC = 2400;              // 40 min upper bound

// Hex helper for tagged session IDs — same shape as _common.randSessionId
// but with a problem-type prefix so the dashboard table can show it as
// the "conversation title".
function tagHex(len) {
  const chars = '0123456789abcdef';
  let s = '';
  for (let i = 0; i < len; i++) s += chars[Math.floor(Math.random() * 16)];
  return s;
}
function taggedSession(prefix) {
  return `sess_${prefix}_${tagHex(12)}`;
}

// ── k6 options ────────────────────────────────────────────────────────────────

export const options = {
  scenarios: {
    anomalies: {
      executor: 'constant-vus',
      vus: 1,                               // serialize problems
      duration: '24h',
      exec: 'session',
    },
  },
};

// ── helpers ───────────────────────────────────────────────────────────────────

function ask(user, sessionId, conversationId, question, opts) {
  const body = {
    question,
    employee_email: user.email,
    employee_name: user.name,
    session_id: sessionId,
    conversation_id: conversationId,
    preferred_provider: 'ollama',
  };
  return request('POST', `${BASE}/api/ask`, body, user, sessionId, opts || {});
}

// ── problem: runaway_loop ─────────────────────────────────────────────────────
//
// User's chat client stuck in a retry/poll loop. Calls per minute spikes
// for 180 seconds, then stops abruptly (as if the loop crashed). Peak
// drawn from a wide range so successive bursts look visibly distinct on
// the dashboard.
function runRunawayLoop(user) {
  const durationSec = 180;
  const callsPerMin = randInt(5, 80);
  const intervalMs = Math.max(50, Math.round(60_000 / callsPerMin));
  const conv = randConversationId();
  const session = taggedSession('anomrun');
  const startMs = Date.now();
  console.log(`[anomaly] runaway_loop user=${user.email} target=${callsPerMin}/min duration=${durationSec}s session=${session}`);
  let count = 0;
  while ((Date.now() - startMs) < durationSec * 1000) {
    ask(user, session, conv, "is anyone there? can you help with my last question?", { timeout: '30s' });
    count++;
    sleep(intervalMs / 1000);
  }
  console.log(`[anomaly] runaway_loop user=${user.email} done count=${count}`);
}

// ── problem: token_glutton ────────────────────────────────────────────────────
//
// User asks for huge documents — output token usage spikes for ~2 minutes.
// We bake the magnitude into the prompt (Nx-page report) so Ollama actually
// emits a proportionally large response; the call rate also varies a bit
// so successive bursts don't look identical on the dashboard.
function runTokenGlutton(user) {
  const durationSec = 120;
  const multiplier = randInt(5, 80);
  const callsPerMin = randInt(20, 80);
  const conv = randConversationId();
  const session = taggedSession('anomglut');
  const intervalMs = Math.round(60_000 / callsPerMin);
  const huge =
    `Write me a complete ${multiplier}-page internal knowledge-base article ` +
    `covering everything a new senior engineer at Acme needs to onboard. ` +
    `Include full code examples for IDE setup, full step-by-step deployment ` +
    `walkthroughs, every internal tool's auth flow, our git policy, review ` +
    `policy, deployment policy, incident runbook, oncall handoff template, ` +
    `the entire AWS topology, every Postgres table we use, every Slack ` +
    `channel that matters, the new-hire scavenger hunt, and detailed bios ` +
    `of every team. Spell EVERY section out in full — do not summarize. ` +
    `I want a complete reference doc with all details inline.`;
  const startMs = Date.now();
  console.log(`[anomaly] token_glutton user=${user.email} pages=${multiplier} rate=${callsPerMin}/min duration=${durationSec}s session=${session}`);
  let count = 0;
  while ((Date.now() - startMs) < durationSec * 1000) {
    ask(user, session, conv, huge, { timeout: '90s' });
    count++;
    sleep(intervalMs / 1000);
  }
  console.log(`[anomaly] token_glutton user=${user.email} done count=${count}`);
}

// ── problem catalog ──────────────────────────────────────────────────────────

const PROBLEMS = [
  { id: 'runaway_loop',  run: runRunawayLoop },
  { id: 'token_glutton', run: runTokenGlutton },
];

// ── entrypoint ────────────────────────────────────────────────────────────────

export function session() {
  if (USERS.length === 0) {
    console.log('[anomaly] no users in pool — sleeping');
    sleep(60);
    return;
  }
  const user = pickOne(USERS);
  const problem = pickOne(PROBLEMS);
  const t0 = Date.now();
  console.log(`[anomaly] iteration start problem=${problem.id} user=${user.email}`);
  try {
    problem.run(user);
  } catch (e) {
    console.log(`[anomaly] problem=${problem.id} threw: ${e}`);
  }
  const elapsedSec = (Date.now() - t0) / 1000;
  // Per-iteration random pause: each gap is independently sampled from
  // [5, 20] minutes so successive bursts don't fall on a predictable cadence.
  const intervalSec = randInt(INTERVAL_MIN_SEC, INTERVAL_MAX_SEC);
  const wait = Math.max(1, intervalSec - elapsedSec);
  console.log(`[anomaly] iteration done elapsed=${elapsedSec.toFixed(1)}s next=${wait.toFixed(1)}s (interval=${intervalSec}s)`);
  sleep(wait);
}
