import logging
import os
import re
import sys

from src.core.cli_identity import new_message_id
from src.core.retry import RetryPolicy, retry_settings

logger = logging.getLogger(__name__)

# OpenCode CLI session id shape: "ses_" + 12 lowercase hex chars + 14
# alphanumerics (30 chars total). ZEN's free-tier gate rejects anything else
# with FreeTierError ("free tier can only be used from within OpenCode") --
# verified live: the exact same request body/headers returns 200 with a
# well-formed id and 403 with a malformed one.
SESSION_ID_RE = re.compile(r"^ses_[0-9a-f]{12}[0-9A-Za-z]{14}$")

# OpenCode project id shape: 40 lowercase hex chars (sha1 sized), the same
# shape the real CLI sends in x-opencode-project.
PROJECT_ID_RE = re.compile(r"^[0-9a-f]{40}$")


def _new_trace_ids() -> tuple[str, str]:
    """W3C trace id (32 hex) and span id (16 hex) for trace propagation."""
    import secrets

    return secrets.token_hex(16), secrets.token_hex(8)



def _new_session_id():
    """Generate a CLI-shaped OpenCode session id.

    Shape: ``ses_`` + 12 lowercase hex chars + 14 mixed-case alphanumerics.
    The old generator emitted 24 arbitrary alphanumerics, which the ZEN
    free-tier gate rejects (the 12-hex part must be lowercase hex).
    """
    import secrets
    import string

    hex_alphabet = "0123456789abcdef"
    alnum_alphabet = string.ascii_letters + string.digits
    prefix = "".join(secrets.choice(hex_alphabet) for _ in range(12))
    suffix = "".join(secrets.choice(alnum_alphabet) for _ in range(14))
    return f"ses_{prefix}{suffix}"


def _coerce_session_id(raw):
    """Return a gate-valid session id for ``raw`` (or a freshly minted one).

    An inherited environment variable (e.g. exported in the shell that
    started the proxy) beats .env, so a malformed OPENCODE_SESSION_ID can
    wedge the proxy into permanent 403s. Never trust it blindly: fall back
    to a valid .env value, then to a freshly minted id.
    """
    candidate = (raw or "").strip()
    if candidate and SESSION_ID_RE.match(candidate):
        return candidate
    if candidate:
        logger.warning(
            "OPENCODE_SESSION_ID %r does not match the CLI session id format "
            "(ses_ + 12 lowercase hex + 14 alphanumerics); ZEN's free-tier "
            "gate would reject it with 403 FreeTierError -- falling back",
            candidate,
        )
    # .env value (if any) is the next best source: it is the operator's
    # pinned, known-good CLI id.
    try:
        from dotenv import dotenv_values

        env_value = (dotenv_values().get("OPENCODE_SESSION_ID") or "").strip()
        if env_value and SESSION_ID_RE.match(env_value):
            logger.warning("using the .env OPENCODE_SESSION_ID instead")
            return env_value
    except Exception:  # pragma: no cover - dotenv missing/unreadable
        pass
    logger.warning("minting a fresh CLI-shaped session id")
    return _new_session_id()


# Configuration
class Config:
    def __init__(self):
        self.openai_api_key = os.environ.get("OPENAI_API_KEY")
        if not self.openai_api_key:
            raise ValueError("OPENAI_API_KEY not found in environment variables")
        
        # Add Anthropic API key for client validation
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not self.anthropic_api_key:
            print("Warning: ANTHROPIC_API_KEY not set. Client API key validation will be disabled.")
        
        self.openai_base_url = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
        self.azure_api_version = os.environ.get("AZURE_API_VERSION")  # For Azure OpenAI
        self.host = os.environ.get("HOST", "0.0.0.0")
        self.port = int(os.environ.get("PORT", "8082"))
        # Optional customer-facing API origin shown on the dashboard. When
        # empty the UI derives it from the browser origin + proxy port.
        self.public_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
        self.log_level = os.environ.get("LOG_LEVEL", "INFO")
        self.max_tokens_limit = int(os.environ.get("MAX_TOKENS_LIMIT", "4096"))
        self.min_tokens_limit = int(os.environ.get("MIN_TOKENS_LIMIT", "100"))
        
        # Connection settings
        self.request_timeout = int(os.environ.get("REQUEST_TIMEOUT", "90"))
        self.max_retries = int(os.environ.get("MAX_RETRIES", "2"))
        
        # Model settings - BIG and SMALL models
        self.big_model = os.environ.get("BIG_MODEL", "gpt-4o")
        self.middle_model = os.environ.get("MIDDLE_MODEL", self.big_model)
        self.small_model = os.environ.get("SMALL_MODEL", "gpt-4o-mini")

        # Upstream wire API: "chat" (default, /chat/completions) or
        # "responses" (/responses passthrough-style translation).
        # Some free-tier models (e.g. Muse Spark on OpenCode ZEN) are
        # responses-only upstream and 500 on chat/completions.
        self.upstream_wire_api = os.environ.get("UPSTREAM_WIRE_API", "chat").strip().lower()
        if self.upstream_wire_api not in ("chat", "responses"):
            print(f"Warning: unknown UPSTREAM_WIRE_API '{self.upstream_wire_api}', falling back to 'chat'.")
            self.upstream_wire_api = "chat"

        # Upstream User-Agent. ZEN rate-limits by UA: only opencode client UAs
        # get the normal free tier, everything else is treated as bot traffic.
        # The gate also enforces CLI *freshness* (a stale pinned version gets
        # 403 FreeTierError), so resolve the current opencode release from the
        # npm registry with a daily cache. UPSTREAM_USER_AGENT still overrides.
        self.upstream_user_agent = os.environ.get("UPSTREAM_USER_AGENT", "").strip() or self._fresh_opencode_ua()

        # OpenCode client identity headers. ZEN's free tier rejects requests
        # without x-opencode-session ("MissingSessionID: free tier can only
        # be used in OpenCode"). Mirrors what the real OpenCode client sends:
        #   x-opencode-session, x-opencode-project, x-opencode-request (user),
        #   x-opencode-client, User-Agent: opencode/<version>
        # The server only checks presence/format (it cannot know local
        # session ids in advance), so an auto-generated stable id per proxy
        # start works. Pin via OPENCODE_SESSION_ID to reuse one.
        self.opencode_session_id = _coerce_session_id(os.environ.get("OPENCODE_SESSION_ID"))
        self.opencode_project_id = os.environ.get("OPENCODE_PROJECT_ID", "global")
        if not PROJECT_ID_RE.match(self.opencode_project_id or ""):
            # The real CLI sends a 40 hex char project id; the gate expects
            # that shape, so derive a stable one from whatever is configured.
            import hashlib

            self.opencode_project_id = hashlib.sha1(
                (self.opencode_project_id or "global").encode()
            ).hexdigest()
        self.opencode_client = os.environ.get("OPENCODE_CLIENT", "cli")
        self.opencode_user_id = os.environ.get("OPENCODE_USER_ID", "")

        # Floor for max_output_tokens on the responses path: reasoning models
        # burn tokens before producing visible output.
        self.responses_min_output_tokens = int(os.environ.get("RESPONSES_MIN_OUTPUT_TOKENS", "2048"))

        # How long (seconds) the responses path keeps retrying retryable
        # upstream failures (5xx) with exponential backoff before giving up.
        # Muse Spark free tier flaps; the CLI retries too, but absorbing
        # short outages here avoids CLI-visible errors entirely.
        self.responses_retry_budget_secs = float(os.environ.get("RESPONSES_RETRY_BUDGET_SECS", "120"))

        # Transient-failure retry policy shared by every upstream path.
        # The free tier's models flap ("Model is temporarily unavailable
        # upstream", bare 5xx, dropped connections); those failures clear on
        # their own within seconds, so the proxy retries them in place
        # instead of surfacing an error to the client. A FIXED interval
        # (not exponential backoff) matches how long the client is willing
        # to wait, and the count caps the total wait.
        self.retry_attempts, self.retry_interval_secs = retry_settings()
        self.retry_policy = RetryPolicy(self.retry_attempts, self.retry_interval_secs)

        # SSE keepalive interval (seconds) on the responses streaming path.
        # Reasoning upstreams can go silent for minutes; Claude Code shows
        # "Waiting for API response" after ~20s of no bytes and aborts the
        # stream at its idle watchdogs. Periodic ping events reset those
        # timers. 0 disables. Note: this is byte-level liveness only, it
        # does not extend prompt-cache TTL (cache hits need a real request
        # inside the 5m/1h window).
        self.stream_keepalive_secs = float(os.environ.get("STREAM_KEEPALIVE_SECS", "15"))

        # P5: default cache TTL when the client sets no breakpoint.
        # "5m" matches current Anthropic default; set "1h" only if your
        # upstream honors extended TTL (otherwise cost would understate).
        self.cache_ttl_default = os.environ.get("CACHE_TTL_DEFAULT", "5m").strip().lower()
        if self.cache_ttl_default not in ("5m", "1h"):
            print(f"Warning: unknown CACHE_TTL_DEFAULT '{self.cache_ttl_default}', falling back to '5m'.")
            self.cache_ttl_default = "5m"

    def _fresh_opencode_ua(self, pinned: str = "opencode/latest/1.19.3/cli", ttl_secs: int = 86400) -> str:
        """Latest ``opencode/latest/<version>/cli`` UA, cached ~daily.

        This is the exact User-Agent shape the real OpenCode CLI sends
        (captured from a live CLI request). The version comes from the
        locally installed CLI when available, otherwise from the npm
        registry. Never raises: any failure (offline, timeout, parse) falls
        back to the last cached value, then the pinned default.
        """
        import json as _json
        from pathlib import Path

        cache = Path.home() / ".cache" / "opencode-ua" / "version.txt"
        try:
            import time as _time
            if cache.exists() and (_time.time() - cache.stat().st_mtime) < ttl_secs:
                cached = cache.read_text().strip()
                if cached.startswith("opencode/latest/"):
                    return cached
        except OSError:
            pass
        version = self._local_opencode_version()
        if not version:
            try:
                import httpx
                r = httpx.get(
                    "https://registry.npmjs.org/opencode-ai/latest",
                    timeout=3.0,
                    headers={"User-Agent": "claude-code-proxy/version-probe"},
                )
                version = str(_json.loads(r.text)["version"]).strip()
            except Exception as e:
                logger.debug(f"opencode version probe failed: {e}")
        if version:
            ua = f"opencode/latest/{version}/cli"
            try:
                cache.parent.mkdir(parents=True, exist_ok=True)
                cache.write_text(ua)
            except OSError:
                pass
            return ua
        # Serve stale cache if fresh fetch failed but an old one exists.
        try:
            cached = cache.read_text().strip()
            if cached:
                return cached
        except OSError:
            pass
        return pinned

    @staticmethod
    def _local_opencode_version() -> str:
        """Version of the locally installed OpenCode CLI, if any."""
        import re
        import shutil
        import subprocess

        binary = shutil.which("opencode") or "/home/ubuntu/.opencode/bin/opencode"
        try:
            out = subprocess.run(
                [binary, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout
        except Exception:
            return ""
        match = re.search(r"v?(\d+\.\d+\.\d+)", out)
        return match.group(1) if match else ""

        
    def validate_api_key(self):
        """Basic API key validation"""
        if not self.openai_api_key:
            return False
        # Basic format check for OpenAI API keys
        if not self.openai_api_key.startswith('sk-'):
            return False
        return True
        
    def validate_client_api_key(self, client_api_key):
        """Validate client's Anthropic API key"""
        # If no ANTHROPIC_API_KEY is set in environment, skip validation
        if not self.anthropic_api_key:
            return True

        # Check if the client's API key matches the expected value
        import hmac

        return hmac.compare_digest(str(client_api_key), str(self.anthropic_api_key))
    
    def get_custom_headers(self):
        """Get custom headers from environment variables"""
        custom_headers = {}
        
        # Get all environment variables
        env_vars = dict(os.environ)
        
        # Find CUSTOM_HEADER_* environment variables
        for env_key, env_value in env_vars.items():
            if env_key.startswith('CUSTOM_HEADER_'):
                # Convert CUSTOM_HEADER_KEY to Header-Key
                # Remove 'CUSTOM_HEADER_' prefix and convert to header format
                header_name = env_key[14:]  # Remove 'CUSTOM_HEADER_' prefix
                
                if header_name:  # Make sure it's not empty
                    # Convert underscores to hyphens for HTTP header format
                    header_name = header_name.replace('_', '-')
                    custom_headers[header_name] = env_value

        return custom_headers

    def get_upstream_headers(self):
        """OpenCode identity headers sent upstream with every ZEN request.

        A fresh ``x-opencode-request`` message id is minted on every call:
        the free-tier gate expects the header present in CLI (``msg_``)
        format. The session id stays pinned (set OPENCODE_SESSION_ID to a
        genuine CLI-generated ``ses_`` id -- values are reusable).

        The header set below mirrors what the real OpenCode CLI sends
        (captured from a live CLI request): the gate rejects requests that
        omit the ``x-opencode-*`` family, the session affinity headers or the
        trace propagation headers, answering 403 FreeTierError.
        """
        headers = {"User-Agent": self.upstream_user_agent}
        session_id = self.opencode_session_id
        if session_id:
            headers["x-opencode-session"] = session_id
            headers["x-opencode-session-id"] = session_id
            headers["x-session-affinity"] = session_id
            headers["x-session-id"] = session_id
        if self.opencode_project_id:
            headers["x-opencode-project"] = self.opencode_project_id
        if self.opencode_client:
            headers["x-opencode-client"] = self.opencode_client
        headers["x-opencode-request"] = (
            self.opencode_user_id or new_message_id()
        )
        trace_id, span_id = _new_trace_ids()
        headers["traceparent"] = f"00-{trace_id}-{span_id}-01"
        headers["b3"] = f"{trace_id}-{span_id}-1-{trace_id}"
        return headers

try:
    config = Config()
    print(f" Configuration loaded: API_KEY={'*' * 20}..., BASE_URL='{config.openai_base_url}'")
except Exception as e:
    print(f"=4 Configuration Error: {e}")
    sys.exit(1)
