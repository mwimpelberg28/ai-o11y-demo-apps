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
  request, loadUsers, randInt, pickOne, randSessionId, randConversationId,
} from './_common.js';

// ── init context ──────────────────────────────────────────────────────────────

const USERS = loadUsers();                  // already sliced to 5 by orchestrator
const BASE = __ENV.SB_BASE_URL || 'http://sb-web.support-bot.svc.cluster.local';
const INTERVAL_SEC = 600;                   // 10 minutes between problems

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
// User's chat client stuck in a retry/poll loop. Calls per minute jumps
// 10-50x for 180 seconds, then stops abruptly (as if the loop crashed).
function runRunawayLoop(user) {
  const durationSec = 180;
  const callsPerMin = randInt(10, 50);
  const intervalMs = Math.max(50, Math.round(60_000 / callsPerMin));
  const conv = randConversationId();
  const session = randSessionId();
  const startMs = Date.now();
  console.log(`[anomaly] runaway_loop user=${user.email} target=${callsPerMin}/min duration=${durationSec}s`);
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
// User asks for huge documents — output token usage spikes 10-50x for 2 min.
// We bake the magnitude into the prompt (Nx-page report) so Ollama actually
// emits a proportionally large response.
function runTokenGlutton(user) {
  const durationSec = 120;
  const multiplier = randInt(10, 50);
  const conv = randConversationId();
  const session = randSessionId();
  const callsPerMin = 60;                   // ~1 req/sec
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
  console.log(`[anomaly] token_glutton user=${user.email} multiplier=${multiplier}x duration=${durationSec}s`);
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
  const wait = Math.max(1, INTERVAL_SEC - elapsedSec);
  console.log(`[anomaly] iteration done elapsed=${elapsedSec.toFixed(1)}s next=${wait.toFixed(1)}s`);
  sleep(wait);
}
