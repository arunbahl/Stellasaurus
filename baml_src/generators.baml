// Generates the typed Python client into src/stellasaurus/baml_client.
// Regenerate after editing any .baml file:  uv run baml-cli generate
generator python {
  output_type "python/pydantic"
  output_dir "../src/stellasaurus"
  version "0.222.0"
}
