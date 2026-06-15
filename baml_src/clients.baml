// Default LLM client: Fireworks AI (DeepSeek V4 Flash) via its OpenAI-compatible
// endpoint. Auth is passed as a full Authorization header, so:
//   FIREWORKS_LLM_ENDPOINT   e.g. https://api.fireworks.ai/inference/v1
//   FIREWORKS_API_KEY_BAML   the FULL header value, e.g. "Bearer fw_..."
// The BAML VSCode playground reads .env automatically; the app loads it at startup.
// Other endpoints/models will be added and tested later.
// Retry transient failures AND malformed/parse-failed completions (the model is
// occasionally non-compliant with the JSON schema). Added on top of the client
// definition; remove if undesired.
retry_policy FireworksRetry {
  max_retries 3
  strategy {
    type exponential_backoff
    delay_ms 300
    multiplier 2.0
    max_delay_ms 3000
  }
}

client<llm> Fireworks_DeepSeekV4Flash {
  provider "openai-generic"
  options {
    model "accounts/fireworks/models/deepseek-v4-flash"
    base_url env.FIREWORKS_LLM_ENDPOINT
    temperature 0.1
    headers {
      "Authorization" env.FIREWORKS_API_KEY_BAML
    }
  }
  retry_policy FireworksRetry
}
