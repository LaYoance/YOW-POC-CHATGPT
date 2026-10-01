"""
Tier 1 Feature Tests: SPA Routing & Conversation Deep Linking.
Verifies /c/{conversation_id} routing and history deep-link support.
"""

from starlette.testclient import TestClient
from app.main import app

def test_conversation_spa_routes():
    client = TestClient(app)

    # Root route
    res_root = client.get("/")
    assert res_root.status_code == 200
    assert "ChatGPT POC" in res_root.text

    # Conversation deep-link route
    conv_id = "6abe3496-c948-83eb-90cd-bff5acb2652f"
    res_conv = client.get(f"/c/{conv_id}")
    assert res_conv.status_code == 200
    assert "ChatGPT POC" in res_conv.text

    # Conversation deep-link route with trailing slash
    res_conv_slash = client.get(f"/c/{conv_id}/")
    assert res_conv_slash.status_code == 200
    assert "ChatGPT POC" in res_conv_slash.text
