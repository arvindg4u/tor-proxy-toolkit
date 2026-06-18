import http from 'node:http';
import https from 'node:https';
import crypto from 'node:crypto';

const PORT = 7860;
const MIMO_BASE = 'api.xiaomimimo.com';
const MIMO_API_KEY = process.env.MIMO_API_KEY || 'sk-your-mimo-api-key';

// Convert responses API request → chat/completions request
function responsesToChat(body) {
  const messages = [];

  // System instructions
  if (body.instructions) {
    messages.push({ role: 'system', content: body.instructions });
  }

  // Input messages
  if (typeof body.input === 'string') {
    messages.push({ role: 'user', content: body.input });
  } else if (Array.isArray(body.input)) {
    for (const item of body.input) {
      if (item.type === 'message') {
        const role = item.role === 'developer' ? 'system' : item.role;
        const content = Array.isArray(item.content)
          ? item.content.filter(c => c.type === 'input_text').map(c => c.text).join('\n')
          : item.content;
        messages.push({ role, content });
      }
    }
  }

  // Model
  let model = body.model || 'mimo-auto';
  if (!model || model === 'mimo-auto' || model === 'mimo/mimo-auto') {
    model = 'mimo-v2.5';
  }

  const chatBody = {
    model,
    messages,
    max_completion_tokens: body.max_output_tokens || 16384,
  };

  // Reasoning effort → temperature hint
  if (body.reasoning?.effort === 'high') {
    chatBody.temperature = 0.7;
  } else if (body.reasoning?.effort === 'low') {
    chatBody.temperature = 0.3;
  }

  // Tools
  if (body.tools?.length > 0) {
    chatBody.tools = body.tools.map(t => {
      if (t.type === 'function') {
        return {
          type: 'function',
          function: {
            name: t.name,
            description: t.description || '',
            parameters: t.parameters || {},
          }
        };
      }
      return t;
    });
    chatBody.tool_choice = body.tool_choice === 'auto' ? 'auto' : 'auto';
  }

  return chatBody;
}

// Convert chat/completions response → responses API format
function chatToResponses(chatRes, requestId) {
  const msg = chatRes.choices?.[0]?.message;
  if (!msg) {
    return {
      id: requestId,
      object: 'response',
      status: 'completed',
      output: [],
      usage: { input_tokens: 0, output_tokens: 0, total_tokens: 0 },
    };
  }

  const output = [];

  // Reasoning item
  if (msg.reasoning_content) {
    output.push({
      type: 'reasoning',
      id: `reason_${crypto.randomUUID().slice(0, 12)}`,
      summary: [{ type: 'summary_text', text: msg.reasoning_content }],
    });
  }

  // Message output
  output.push({
    type: 'message',
    id: `msg_${crypto.randomUUID().slice(0, 12)}`,
    role: 'assistant',
    status: 'completed',
    content: [{
      type: 'output_text',
      text: msg.content || '',
    }],
  });

  // Tool calls
  if (msg.tool_calls?.length > 0) {
    for (const tc of msg.tool_calls) {
      output.push({
        type: 'function_call',
        id: `fc_${crypto.randomUUID().slice(0, 12)}`,
        call_id: tc.id,
        name: tc.function.name,
        arguments: tc.function.arguments,
        status: 'completed',
      });
    }
  }

  const usage = chatRes.usage || {};

  return {
    id: requestId,
    object: 'response',
    status: 'completed',
    output,
    usage: {
      input_tokens: usage.prompt_tokens || 0,
      output_tokens: usage.completion_tokens || 0,
      total_tokens: usage.total_tokens || 0,
      input_tokens_details: { cached_tokens: usage.prompt_tokens_details?.cached_tokens || 0 },
      output_tokens_details: { reasoning_tokens: usage.completion_tokens_details?.reasoning_tokens || 0 },
    },
  };
}

const server = http.createServer(async (req, res) => {
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
  res.setHeader('Access-Control-Allow-Headers', '*');
  if (req.method === 'OPTIONS') { res.writeHead(204); return res.end(); }

  // Health check
  if (req.url === '/health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({ status: 'ok', provider: 'mimo-proxy', wire_api: 'responses' }));
  }

  // Models list
  if (req.url === '/v1/models' && req.method === 'GET') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    return res.end(JSON.stringify({
      object: 'list',
      data: [
        { id: 'mimo-auto', object: 'model', owned_by: 'xiaomi' },
        { id: 'mimo-v2.5', object: 'model', owned_by: 'xiaomi' },
        { id: 'mimo-v2.5-pro', object: 'model', owned_by: 'xiaomi' },
        { id: 'mimo-v2-flash', object: 'model', owned_by: 'xiaomi' },
      ]
    }));
  }

  // Proxy responses API → chat/completions
  if (req.url === '/v1/responses' && req.method === 'POST') {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', () => {
      const bodyStr = Buffer.concat(body).toString();
      let responsesBody;
      try { responsesBody = JSON.parse(bodyStr); } catch { responsesBody = {}; }

      const requestId = `resp_${crypto.randomUUID().slice(0, 12)}`;
      const chatBody = responsesToChat(responsesBody);

      console.log(`[proxy] ${requestId} model=${chatBody.model} tools=${chatBody.tools?.length || 0}`);

      const payload = JSON.stringify(chatBody);

      const options = {
        hostname: MIMO_BASE,
        port: 443,
        path: '/v1/chat/completions',
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${MIMO_API_KEY}`,
          'Content-Length': Buffer.byteLength(payload),
        },
      };

      const proxyReq = https.request(options, (proxyRes) => {
        let data = [];
        proxyRes.on('data', chunk => data.push(chunk));
        proxyRes.on('end', () => {
          const raw = Buffer.concat(data).toString();
          try {
            const chatRes = JSON.parse(raw);
            if (chatRes.error) {
              res.writeHead(proxyRes.statusCode, { 'Content-Type': 'application/json' });
              return res.end(JSON.stringify({
                type: 'error',
                error: { type: 'api_error', message: chatRes.error.message || 'Unknown error' },
              }));
            }
            const responsesRes = chatToResponses(chatRes, requestId);
            res.writeHead(200, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify(responsesRes));
          } catch (e) {
            res.writeHead(502, { 'Content-Type': 'application/json' });
            res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message: e.message } }));
          }
        });
      });

      proxyReq.on('error', (err) => {
        res.writeHead(502, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ type: 'error', error: { type: 'api_error', message: err.message } }));
      });

      proxyReq.write(payload);
      proxyReq.end();
    });
    return;
  }

  // Fallback: pass through any /v1/* as chat/completions
  if (req.url.startsWith('/v1/')) {
    let body = [];
    req.on('data', chunk => body.push(chunk));
    req.on('end', () => {
      let bodyStr = Buffer.concat(body).toString();
      try {
        const parsed = JSON.parse(bodyStr);
        if (!parsed.model || parsed.model === 'mimo-auto' || parsed.model === 'mimo/mimo-auto') {
          parsed.model = 'mimo-v2.5';
        }
        bodyStr = JSON.stringify(parsed);
      } catch {}

      const options = {
        hostname: MIMO_BASE,
        port: 443,
        path: req.url,
        method: req.method,
        headers: {
          'Content-Type': 'application/json',
          'Authorization': `Bearer ${MIMO_API_KEY}`,
          'Content-Length': Buffer.byteLength(bodyStr),
        },
      };

      const proxyReq = https.request(options, (proxyRes) => {
        res.writeHead(proxyRes.statusCode, proxyRes.headers);
        proxyRes.pipe(res);
      });
      proxyReq.on('error', (err) => {
        res.writeHead(502, { 'Content-Type': 'application/json' });
        res.end(JSON.stringify({ error: { message: err.message } }));
      });
      proxyReq.write(bodyStr);
      proxyReq.end();
    });
    return;
  }

  res.writeHead(404);
  res.end('Not found');
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`🦜 MiMo Proxy (responses API) on http://127.0.0.1:${PORT}`);
  console.log(`   responses API → chat/completions conversion: ON`);
  console.log(`   API Key: ${MIMO_API_KEY ? 'SET ✓' : 'NOT SET ✗'}`);
});
