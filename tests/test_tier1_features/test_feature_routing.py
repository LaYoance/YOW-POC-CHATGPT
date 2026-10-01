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

    # Project deep-link route
    proj_id = "g-p-6aad8a7e9d148191b77a9c5de7b40c44"
    proj_slug = "g-p-6aad8a7e9d148191b77a9c5de7b40c44-yow-your-operating-workspace"
    res_proj = client.get(f"/g/{proj_slug}")
    assert res_proj.status_code == 200
    assert "ChatGPT POC" in res_proj.text

    # Project with trailing slash
    res_proj_slash = client.get(f"/g/{proj_slug}/")
    assert res_proj_slash.status_code == 200
    assert "ChatGPT POC" in res_proj_slash.text

    # Project + Conversation deep-link route
    res_proj_conv = client.get(f"/g/{proj_slug}/c/{conv_id}")
    assert res_proj_conv.status_code == 200
    assert "ChatGPT POC" in res_proj_conv.text

    # Project alias /p/ route
    res_p_conv = client.get(f"/p/{proj_id}/c/{conv_id}")
    assert res_p_conv.status_code == 200
    assert "ChatGPT POC" in res_p_conv.text

