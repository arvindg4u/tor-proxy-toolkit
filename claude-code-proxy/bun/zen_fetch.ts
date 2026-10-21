// Persistent upstream transport for the Claude Code proxy.
//
// WHY BUN: OpenCode ZEN's free tier does not only look at headers, it also
// fingerprints the TLS ClientHello. Python's httpx (OpenSSL) is rejected
// with 403 FreeTierError even when every header and the body are cloned
// from a real OpenCode CLI request. Bun's BoringSSL stack matches the real
// CLI and is accepted.
//
// PROTOCOL: newline delimited JSON over stdin/stdout, one request per line,
// so a single Bun process can serve many concurrent upstream calls.
//   -> {"id":1,"method":"POST","url":"...","headers":{},"body":"<base64>"}
//   <- {"id":1,"status":200,"headers":{}}
//   <- {"id":1,"chunk":"<base64>"}   (repeated, streamed)
//   <- {"id":1,"end":true}
//   <- {"id":1,"error":"..."}        (on failure)

type Request = {
  id: number;
  method: string;
  url: string;
  headers: Record<string, string>;
  body?: string | null;
};

function write(payload: unknown): void {
  process.stdout.write(JSON.stringify(payload) + "\n");
}

async function handle(request: Request): Promise<void> {
  try {
    const response = await fetch(request.url, {
      method: request.method,
      headers: request.headers,
      body: request.body ? Buffer.from(request.body, "base64") : undefined,
    });

    const headers: Record<string, string> = {};
    response.headers.forEach((value, key) => {
      headers[key] = value;
    });
    write({ id: request.id, status: response.status, headers });

    if (response.body) {
      const reader = response.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        write({ id: request.id, chunk: Buffer.from(value).toString("base64") });
      }
    }
    write({ id: request.id, end: true });
  } catch (error) {
    write({ id: request.id, error: String((error as Error)?.stack ?? error) });
  }
}

let buffer = "";
process.stdin.on("end", () => {
  // the parent proxy went away: exit instead of lingering
  process.exit(0);
});
process.stdin.on("data", (chunk: Buffer) => {
  buffer += chunk.toString("utf8");
  let newline = buffer.indexOf("\n");
  while (newline >= 0) {
    const line = buffer.slice(0, newline);
    buffer = buffer.slice(newline + 1);
    if (line.trim().length > 0) {
      try {
        void handle(JSON.parse(line) as Request);
      } catch {
        // ignore malformed lines
      }
    }
    newline = buffer.indexOf("\n");
  }
});
