import type { Response } from 'express';

// PROTOCOL §1 error shape: { "error": { "code": "...", "message": "..." } }
export type ErrorCode =
  | 'bad_request'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'conflict'
  | 'unprocessable'
  | 'payload_too_large'
  | 'rate_limited'
  | 'bad_gateway'
  | 'internal';

export function sendError(res: Response, status: number, code: ErrorCode, message: string): void {
  res.status(status).json({ error: { code, message } });
}

export class HttpError extends Error {
  status: number;
  code: ErrorCode;
  constructor(status: number, code: ErrorCode, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}
