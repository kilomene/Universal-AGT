// Task status state machine — PURE functions, zero DB.
// Source of truth: database schema check constraint + PROTOCOL §3.2.
//
//   queued -> claimed | cancelled
//   claimed -> running | failed | cancelled
//   running -> completed | failed | awaiting_approval | cancelled
//   awaiting_approval -> queued (approved) | cancelled (rejected)
//   retrying -> queued
//   completed | failed | cancelled are terminal

export const TASK_STATUSES = [
  'queued',
  'claimed',
  'running',
  'awaiting_approval',
  'completed',
  'failed',
  'cancelled',
  'retrying',
] as const;

export type TaskStatus = (typeof TASK_STATUSES)[number];

const TRANSITIONS: Record<TaskStatus, TaskStatus[]> = {
  queued: ['claimed', 'cancelled'],
  claimed: ['running', 'failed', 'cancelled'],
  running: ['completed', 'failed', 'awaiting_approval', 'cancelled'],
  awaiting_approval: ['queued', 'cancelled'],
  retrying: ['queued'],
  completed: [],
  failed: [],
  cancelled: [],
};

export function isTaskStatus(s: string): s is TaskStatus {
  return (TASK_STATUSES as readonly string[]).includes(s);
}

export function allowedTaskTransitions(from: string): string[] {
  if (!isTaskStatus(from)) return [];
  return [...TRANSITIONS[from]];
}

export function canTransitionTask(from: string, to: string): boolean {
  return allowedTaskTransitions(from).includes(to);
}

export function isTerminalTaskStatus(s: string): boolean {
  return s === 'completed' || s === 'failed' || s === 'cancelled';
}
