import pytest

from supportguard.config import Settings
from supportguard.main import build_provider
from supportguard.providers.deepseek import ProviderError
from supportguard.providers.fake import DeterministicFakeProvider
from supportguard.rag import embeddings
from supportguard.rag.embeddings import DeterministicEmbedding, build_embedding_provider


def test_production_embedding_uses_only_the_pinned_image_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}
    marker = object()

    def build(
        model_name: str,
        revision: str,
        *,
        local_files_only: bool,
    ) -> object:
        captured.update(
            model_name=model_name,
            revision=revision,
            local_files_only=local_files_only,
        )
        return marker

    monkeypatch.setattr(embeddings, "E5SmallEmbedding", build)
    settings = Settings(app_env="production", embedding_mode="e5", _env_file=None)

    assert build_embedding_provider(settings) is marker
    assert captured == {
        "model_name": settings.embedding_model,
        "revision": settings.embedding_revision,
        "local_files_only": True,
    }


def test_runtime_model_defaults_are_frozen() -> None:
    settings = Settings(_env_file=None)

    assert settings.llm_base_url == "https://api.deepseek.com"
    assert settings.llm_model == "deepseek-v4-flash"
    assert settings.llm_thinking_enabled is False
    assert settings.llm_temperature == 0
    assert settings.provider_max_input_tokens == 16_000
    assert settings.provider_max_output_tokens == 2_000
    assert settings.embedding_mode == "e5"
    assert settings.embedding_model == "intfloat/multilingual-e5-small"


def test_deterministic_embedding_mode_is_explicit_and_forbidden_in_production() -> None:
    development = Settings(_env_file=None, embedding_mode="deterministic-fixture")
    assert isinstance(build_embedding_provider(development), DeterministicEmbedding)

    production = Settings(
        _env_file=None,
        app_env="production",
        auth_mode="production",
        embedding_mode="deterministic-fixture",
    )
    with pytest.raises(RuntimeError, match="cannot enable deterministic fixture embeddings"):
        build_embedding_provider(production)


def test_runtime_never_silently_falls_back_to_fake() -> None:
    production = Settings(_env_file=None, app_env="production", deepseek_api_key=None)
    with pytest.raises(ProviderError):
        build_provider(production, testing=False)

    explicit_fake = Settings(
        _env_file=None,
        app_env="development",
        deepseek_api_key=None,
        demo_fake_provider=True,
        provider_max_input_tokens=15_000,
    )
    fake_provider = build_provider(explicit_fake, testing=False)
    assert isinstance(fake_provider, DeterministicFakeProvider)
    assert fake_provider.max_input_tokens == explicit_fake.provider_max_input_tokens

    forbidden_fake = Settings(
        _env_file=None,
        app_env="production",
        auth_mode="production",
        deepseek_api_key="configured",
        demo_fake_provider=True,
    )
    with pytest.raises(ProviderError, match="cannot enable the fake provider"):
        build_provider(forbidden_fake, testing=False)
