// api_client.js - 재시도 로직이 포함된 간단한 REST 클라이언트 (Node.js)
"use strict";

const DEFAULT_TIMEOUT = 8000;

async function sleep(ms) {
  return new Promise((r) => setTimeout(r, ms));
}

class ApiClient {
  constructor(baseUrl, { retries = 3, timeout = DEFAULT_TIMEOUT } = {}) {
    this.baseUrl = baseUrl.replace(/\/$/, "");
    this.retries = retries;
    this.timeout = timeout;
  }

  async request(path, options = {}) {
    let lastErr;
    for (let attempt = 0; attempt <= this.retries; attempt++) {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), this.timeout);
      try {
        const res = await fetch(this.baseUrl + path, { ...options, signal: ctrl.signal });
        clearTimeout(timer);
        if (!res.ok) throw new Error("HTTP " + res.status);
        return await res.json();
      } catch (err) {
        clearTimeout(timer);
        lastErr = err;
        await sleep(300 * Math.pow(2, attempt)); // 지수 백오프
      }
    }
    throw lastErr;
  }

  get(path) { return this.request(path); }
  post(path, body) {
    return this.request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  }
}

module.exports = { ApiClient };
