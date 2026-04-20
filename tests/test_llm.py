import pytest
import os
from unittest.mock import patch, MagicMock
from app.llm_client import gemini_client, AIResponse, GeminiService

@pytest.fixture
def narrator():
    os.environ["GEMINI_API_KEY"] = "fake-key"
    return gemini_client

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
    client = GeminiService()
    response = await client.get_decision("normal", "Alert", {}, {})
    assert isinstance(response, AIResponse)
    assert response.recommended_gate == "Gate A"

@pytest.mark.asyncio
@patch('app.llm_client.genai.Client')
async def test_get_decision_success(mock_client_class, narrator):
    mock_client = mock_client_class.return_value
    mock_response = MagicMock()
    mock_response.text = '{"narrative": "All good", "accessibility_notes": "Ramp available", "crowd_prediction": "Stable", "recommended_gate": "Gate B", "staff_action": "None"}'
    
    # Mocking the async call client.aio.models.generate_content
    async def mock_gen(*args, **kwargs):
        return mock_response
    
    mock_client.aio.models.generate_content = mock_gen
    narrator.client = mock_client
    
    response = await narrator.get_decision("normal", "Alert", {}, {})
    assert response.narrative == "All good"
    assert response.recommended_gate == "Gate B"
