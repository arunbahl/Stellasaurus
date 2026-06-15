// Default LLM client: Fireworks AI via its OpenAI-compatible endpoint.
// Endpoint + key come from the environment (.env in dev; the BAML VSCode
// playground reads .env automatically, and the app loads it at startup):
//   FIREWORKS_LLM_ENDPOINT   e.g. https://api.fireworks.ai/inference/v1
//   FIREWORKS_API_KEY_BAML   the Fireworks API key used for BAML calls
// The model is pinned here (change it to swap models).
client<llm> Fireworks {
  provider openai-generic
  options {
    base_url env.FIREWORKS_LLM_ENDPOINT
    api_key env.FIREWORKS_API_KEY_BAML
    model "accounts/fireworks/models/llama-v3p3-70b-instruct"
    temperature 0.0
  }
}
