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
from infra.plugins import AI_SERVICE

ai = infra.require(AI_SERVICE)
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
                "config": {
                    "default_provider": "openai",
                    "health_probe": True,
                    "providers": {
                        "openai": {
                            "api_key": "sk-...",
                            "base_url": "https://api.openai.com/v1",
                            "timeout": 10,
                        }
                    },
                },
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
Health checks also avoid vendor network calls by default. Set
`health_probe=True` in production to call the configured provider's model-list
API and verify credentials/upstream reachability.

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
