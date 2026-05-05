# AI Plugin

The AI plugin registers service `ai`, an `AIRegistry` with a unified chat interface.

## Models

The public DTOs are:

- `ChatMessage`
- `ToolDefinition`
- `ToolCall`
- `ChatRequest`
- `ChatResponse`
- `ChatChunk`

## Mock Provider

Mock is the default provider and needs no network or API key.

```python
ai = infra.get("ai")
response = await ai.chat_text("hello")
assert response.provider == "mock"
```

## Provider Configuration

```python
settings = InfraSettings(
    infra={
        "plugins": {
            "ai": {
                "enabled": True,
                "config": {"default_provider": "mock"},
            }
        }
    }
)
```

Valid `default_provider` values:

- `mock`
- `openai`
- `anthropic`
- `gemini`

The SDK adapters are lazy. Creating the plugin does not import SDK packages or require keys. The SDK is imported only when that provider is used.

## Optional SDKs

```bash
pip install -e ".[ai-openai]"
pip install -e ".[ai-anthropic]"
pip install -e ".[ai-gemini]"
```

The adapter classes are:

- `OpenAIProvider`
- `AnthropicAIProvider`
- `GeminiAIProvider`

Each adapter accepts `client=None` by default and also accepts a fake client for tests.
