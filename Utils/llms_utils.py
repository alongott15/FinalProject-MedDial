import logging
import os
import time
from typing import List, Dict
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage, AssistantMessage
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

class AzureAIFoundryClient:
    """Enhanced Azure AI Foundry client with improved conversation handling"""
    
    def __init__(self, model_name='gpt-4.1', temperature=0.3, max_tokens=512):
        self.model_name = model_name
        self.temperature = temperature
        self.max_tokens = max_tokens
        
        # Azure AI Foundry configuration
        self.endpoint = os.getenv("AZURE_AI_ENDPOINT")
        self.api_key = os.getenv("AZURE_AI_API_KEY")
        
        if not self.endpoint or not self.api_key:
            logger.error("Azure AI Foundry credentials not found in environment variables.")
            raise ValueError("AZURE_AI_FOUNDRY_ENDPOINT and AZURE_AI_FOUNDRY_API_KEY must be set.")
        
        try:
            self.client = ChatCompletionsClient(
                endpoint=self.endpoint,
                credential=AzureKeyCredential(self.api_key)
            )
            logger.info(f"Loaded Azure AI Foundry model: {model_name}")
        except Exception as e:
            logger.error(f"Failed to initialize Azure AI Foundry client: {e}")
            raise

def load_gpt_model(model_name='gpt-4.1', temperature=0.3, max_tokens=512):
    """
    Load an Azure AI Foundry model.
    Requires AZURE_AI_FOUNDRY_ENDPOINT and AZURE_AI_FOUNDRY_API_KEY.
    """
    return AzureAIFoundryClient(model_name, temperature, max_tokens)

def chat_generate(llm: AzureAIFoundryClient, messages: List[Dict[str, str]], max_retries: int = 5) -> str:
    azure_messages = []
    for msg in messages:
        if msg['role'] == 'system':
            azure_messages.append(SystemMessage(content=msg['content']))
        elif msg['role'] == 'user':
            azure_messages.append(UserMessage(content=msg['content']))
        elif msg['role'] == 'assistant':
            azure_messages.append(AssistantMessage(content=msg['content']))
        else:
            logger.warning(f"Unknown message role: {msg['role']}. Treating as user message.")
            azure_messages.append(UserMessage(content=msg['content']))

    logger.debug(f"[chat_generate] Sending {len(azure_messages)} messages to Azure AI Foundry")

    for attempt in range(max_retries):
        try:
            response = llm.client.complete(
                messages=azure_messages,
                model=llm.model_name,
                temperature=llm.temperature,
                max_tokens=llm.max_tokens,
                top_p=0.95,
                frequency_penalty=0.1,
                presence_penalty=0.1
            )

            text_content = response.choices[0].message.content.strip()
            logger.debug(f"[chat_generate] Azure AI response: {text_content[:100]}...")

            time.sleep(0.5)
            return text_content

        except HttpResponseError as e:
            if e.status_code == 429:
                wait_time = (2 ** attempt) + (attempt * 0.5)
                logger.warning(f"Rate limit hit (429). Retrying in {wait_time:.1f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait_time)
                if attempt == max_retries - 1:
                    logger.error(f"Max retries reached for rate limit error")
                    return "[ERROR: Rate limit exceeded after retries]"
            else:
                logger.error(f"HTTP error during Azure AI invocation: {e}")
                return f"[ERROR: HTTP {e.status_code}]"
        except Exception as e:
            logger.error(f"Error during Azure AI Foundry invocation: {e}")
            return "[ERROR: Could not generate response from Azure AI Foundry]"

    return "[ERROR: Unexpected end of retries]"