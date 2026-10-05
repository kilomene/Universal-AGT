"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.HttpError = void 0;
exports.sendError = sendError;
function sendError(res, status, code, message) {
    res.status(status).json({ error: { code, message } });
}
class HttpError extends Error {
    status;
    code;
    constructor(status, code, message) {
        super(message);
        this.status = status;
        this.code = code;
    }
}
exports.HttpError = HttpError;
