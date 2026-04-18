import pytest
import os
from unittest.mock import patch, MagicMock
from app.llm_client import GeminiNarrator, AIResponse

@pytest.fixture
def narrator():
    # Force mock API key to avoid real external calls during unit tests
    os.environ["GEMINI_API_KEY"] = "fake-key"
    return GeminiNarrator()

def test_mock_response(narrator):
    response = narrator._mock_response("Test alert")
    assert isinstance(response, AIResponse)
    assert "Test alert" in response.narrative
    assert response.recommended_gate == "Gate A"
    assert "accessible" in response.accessibility_notes

@pytest.mark.asyncio
async def test_get_decision_fallback():
    # If API key is empty, it should use the fallback
    os.environ["GEMINI_API_KEY"] = ""
    client = GeminiNarrator()
    
    response = await client.get_decision("normal", "Alert", {}, {})
    assert isinstance(response, AIResponse)
    assert response.recommended_gate == "Gate A"

@pytest.mark.asyncio
@patch('app.llm_client.httpx.AsyncClient')
async def test_get_decision_success(mock_client_class, narrator):
    mock_client = MagicMock()
    mock_response = MagicMock()
    
    # Mock the JSON structure returned by Gemini API
    mock_response.json.return_value = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": '{"narrative": "All good", "accessibility_notes": "Ramp available", "crowd_prediction": "Stable", "recommended_gate": "Gate B", "staff_action": "None"}'
                }]
            }
        }]
    }
    mock_response.raise_for_status = MagicMock()
    
    async def mock_post(*args, **kwargs):
        return mock_response
    
    mock_client.post = mock_post
    mock_client.__aenter__.return_value = mock_client
    mock_client_class.return_value = mock_client
    
    response = await narrator.get_decision("normal", "Alert", {}, {})
    assert response.narrative == "All good"
    assert response.recommended_gate == "Gate B"
