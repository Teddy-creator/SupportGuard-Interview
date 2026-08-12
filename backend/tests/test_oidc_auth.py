import json
from datetime import UTC, datetime, timedelta

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jwt.algorithms import RSAAlgorithm

from supportguard.api import auth
from supportguard.config import get_settings
from supportguard.main import create_app


def _oidc_fixture(subject: str, *, tenant_id: str = "tenant_demo") -> tuple[str, str]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_jwk = RSAAlgorithm.to_jwk(private_key.public_key(), as_dict=True)
    public_jwk.update({"kid": "test-key", "use": "sig", "alg": "RS256"})
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": subject,
            "tenant_id": tenant_id,
            "iss": "https://issuer.example.test",
            "aud": "supportguard-test",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    return token, json.dumps({"keys": [public_jwk]})


def test_production_oidc_maps_signed_subject_to_membership(monkeypatch) -> None:
    token, jwks = _oidc_fixture("oidc-customer-demo")
    monkeypatch.setenv("AUTH_MODE", "production")
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("OIDC_AUDIENCE", "supportguard-test")
    monkeypatch.setenv("OIDC_JWKS_JSON", jwks)
    monkeypatch.setenv("APP_SECRET_KEY", "production-test-secret-not-default")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "production-internal-test-token")
    async def resolve_fixture(
        _session: object, *, subject: str, tenant_id: str
    ) -> auth.PrincipalResolution:
        return auth.PrincipalResolution(
            schema_version="principal-resolution.v1",
            role="customer",
            subject_id="user_customer_demo",
            tenant_id=tenant_id,
            customer_id="cust_demo",
            membership_role="customer_member",
        )

    monkeypatch.setattr(auth, "resolve_principal_capability", resolve_fixture)
    get_settings.cache_clear()
    try:
        with TestClient(create_app(testing=True)) as client:
            response = client.get("/api/tickets", headers={"Authorization": f"Bearer {token}"})
            demo = client.post(
                "/api/demo-sessions", json={"role": "customer", "customer_id": "cust_demo"}
            )
        assert response.status_code == 200
        assert demo.status_code == 404
    finally:
        get_settings.cache_clear()


def test_production_oidc_rejects_wrong_audience(monkeypatch) -> None:
    token, jwks = _oidc_fixture("oidc-customer-demo")
    monkeypatch.setenv("AUTH_MODE", "production")
    monkeypatch.setenv("OIDC_ISSUER", "https://issuer.example.test")
    monkeypatch.setenv("OIDC_AUDIENCE", "different-audience")
    monkeypatch.setenv("OIDC_JWKS_JSON", jwks)
    monkeypatch.setenv("APP_SECRET_KEY", "production-test-secret-not-default")
    monkeypatch.setenv("INTERNAL_API_TOKEN", "production-internal-test-token")
    get_settings.cache_clear()
    try:
        with TestClient(create_app(testing=True)) as client:
            response = client.get("/api/tickets", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 401
    finally:
        get_settings.cache_clear()
