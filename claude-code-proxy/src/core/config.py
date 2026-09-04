import os
import sys

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
        self.upstream_user_agent = os.environ.get("UPSTREAM_USER_AGENT", "opencode/1.18.18")

        # Floor for max_output_tokens on the responses path: reasoning models
        # burn tokens before producing visible output.
        self.responses_min_output_tokens = int(os.environ.get("RESPONSES_MIN_OUTPUT_TOKENS", "2048"))

        # How long (seconds) the responses path keeps retrying retryable
        # upstream failures (5xx) with exponential backoff before giving up.
        # Muse Spark free tier flaps; the CLI retries too, but absorbing
        # short outages here avoids CLI-visible errors entirely.
        self.responses_retry_budget_secs = float(os.environ.get("RESPONSES_RETRY_BUDGET_SECS", "120"))

        # SSE keepalive interval (seconds) on the responses streaming path.
        # Reasoning upstreams can go silent for minutes; Claude Code shows
        # "Waiting for API response" after ~20s of no bytes and aborts the
        # stream at its idle watchdogs. Periodic ping events reset those
        # timers. 0 disables.
        self.stream_keepalive_secs = float(os.environ.get("STREAM_KEEPALIVE_SECS", "15"))
        
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
        return client_api_key == self.anthropic_api_key
    
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

try:
    config = Config()
    print(f" Configuration loaded: API_KEY={'*' * 20}..., BASE_URL='{config.openai_base_url}'")
except Exception as e:
    print(f"=4 Configuration Error: {e}")
    sys.exit(1)
