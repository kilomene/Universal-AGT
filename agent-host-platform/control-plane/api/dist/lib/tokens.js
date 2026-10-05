"use strict";
Object.defineProperty(exports, "__esModule", { value: true });
exports.sha256Hex = sha256Hex;
exports.generateAgentKey = generateAgentKey;
exports.generateHostToken = generateHostToken;
exports.verifyToken = verifyToken;
const crypto_1 = require("crypto");
function sha256Hex(token) {
    return (0, crypto_1.createHash)('sha256').update(token, 'utf8').digest('hex');
}
function generateAgentKey() {
    const token = 'uag_' + (0, crypto_1.randomBytes)(32).toString('hex');
    return { token, hash: sha256Hex(token) };
}
function generateHostToken() {
    const token = 'uagh_' + (0, crypto_1.randomBytes)(32).toString('hex');
    return { token, hash: sha256Hex(token) };
}
// Constant-time comparison of a presented token against a stored hash.
function verifyToken(presented, storedHash) {
    const presentedHash = sha256Hex(presented);
    const a = Buffer.from(presentedHash, 'hex');
    const b = Buffer.from(storedHash, 'hex');
    if (a.length !== b.length)
        return false;
    return (0, crypto_1.timingSafeEqual)(a, b);
}
