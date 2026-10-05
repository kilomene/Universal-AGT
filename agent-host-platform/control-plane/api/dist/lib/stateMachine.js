"use strict";
// Task status state machine — PURE functions, zero DB.
// Source of truth: database schema check constraint + PROTOCOL §3.2.
//
//   queued -> claimed | cancelled
//   claimed -> running | failed | cancelled
//   running -> completed | failed | awaiting_approval | cancelled
//   awaiting_approval -> queued (approved) | cancelled (rejected)
//   retrying -> queued
//   completed | failed | cancelled are terminal
Object.defineProperty(exports, "__esModule", { value: true });
exports.TASK_STATUSES = void 0;
exports.isTaskStatus = isTaskStatus;
exports.allowedTaskTransitions = allowedTaskTransitions;
exports.canTransitionTask = canTransitionTask;
exports.isTerminalTaskStatus = isTerminalTaskStatus;
exports.TASK_STATUSES = [
    'queued',
    'claimed',
    'running',
    'awaiting_approval',
    'completed',
    'failed',
    'cancelled',
    'retrying',
];
const TRANSITIONS = {
    queued: ['claimed', 'cancelled'],
    claimed: ['running', 'failed', 'cancelled'],
    running: ['completed', 'failed', 'awaiting_approval', 'cancelled'],
    awaiting_approval: ['queued', 'cancelled'],
    retrying: ['queued'],
    completed: [],
    failed: [],
    cancelled: [],
};
function isTaskStatus(s) {
    return exports.TASK_STATUSES.includes(s);
}
function allowedTaskTransitions(from) {
    if (!isTaskStatus(from))
        return [];
    return [...TRANSITIONS[from]];
}
function canTransitionTask(from, to) {
    return allowedTaskTransitions(from).includes(to);
}
function isTerminalTaskStatus(s) {
    return s === 'completed' || s === 'failed' || s === 'cancelled';
}
