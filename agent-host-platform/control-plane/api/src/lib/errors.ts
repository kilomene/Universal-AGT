import type { Request, Response } from 'express';

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
  | 'worker_outdated'
  | 'bad_gateway'
  | 'internal';

export function sendError(res: Response, status: number, code: ErrorCode, message: string): void {
  // §70: code + human-readable message + request id for correlation with
  // the server's request log line (req.req_id, set by the request-id
  // middleware in index.ts and echoed as the x-request-id response
  // header). Never a stack trace, never secrets.
  const error: { code: ErrorCode; message: string; request_id?: string } = { code, message };
  const requestId = (res.req as Request | undefined)?.req_id;
  if (typeof requestId === 'string' && requestId) {
    error.request_id = requestId;
  }
  res.status(status).json({ error });
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
