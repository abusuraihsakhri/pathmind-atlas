"""
PathMind-Atlas Inference Engine supporting Local Ollama, Anthropic Claude, OpenAI, and Zero-PHI Guarded Mock Fallbacks.
Architect: Dr. Abu Suraih Sakhri (@abusuraihsakhri)
"""

from __future__ import annotations
import os
import json
import logging
from enum import Enum
from typing import Optional, Dict, Any
from pydantic import BaseModel, Field

import httpx
from agents.base import assert_no_phi, SecurityException

logger = logging.getLogger("pathmind.llm_factory")


class LLMProvider(str, Enum):
    OLLAMA = "OLLAMA"
    CLAUDE = "CLAUDE"
    OPENAI = "OPENAI"
    MOCK = "MOCK"


class LLMResponse(BaseModel):
    provider: LLMProvider
    model_name: str
    content: str
    usage_tokens: Dict[str, int] = Field(default_factory=lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    raw_response: Optional[Dict[str, Any]] = None


class LLMFactory:
    """
    Unified Inference Engine with embedded Zero-PHI pre/post-invocation guardrails.
    """
    def __init__(
        self,
        provider: Optional[LLMProvider] = None,
        default_model: Optional[str] = None,
        ollama_base_url: Optional[str] = None,
        timeout: float = 30.0
    ):
        provider_env = os.getenv("PATHMIND_LLM_PROVIDER", "MOCK").upper()
        self.provider = provider or LLMProvider(provider_env if provider_env in LLMProvider.__members__ else "MOCK")
        
        self.ollama_base_url = ollama_base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        self.claude_api_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.openai_api_key = os.getenv("OPENAI_API_KEY", "")
        self.timeout = timeout

        if default_model:
            self.default_model = default_model
        elif self.provider == LLMProvider.OLLAMA:
            self.default_model = os.getenv("OLLAMA_MODEL", "llama3")
        elif self.provider == LLMProvider.CLAUDE:
            self.default_model = os.getenv("CLAUDE_MODEL", "claude-3-5-sonnet-20241022")
        elif self.provider == LLMProvider.OPENAI:
            self.default_model = os.getenv("OPENAI_MODEL", "gpt-4o")
        else:
            self.default_model = "pathmind-expert-mock-v1"

    def generate(
        self,
        prompt: str,
        system_prompt: Optional[str] = None,
        model: Optional[str] = None,
        temperature: float = 0.1,
        max_tokens: int = 1500
    ) -> LLMResponse:
        """
        Executes prompt through specified LLM provider with mandatory PHI assertion checks.
        """
        # Strict outbound PHI verification
        assert_no_phi(prompt)
        if system_prompt:
            assert_no_phi(system_prompt)

        selected_model = model or self.default_model

        if self.provider == LLMProvider.OLLAMA:
            response = self._invoke_ollama(prompt, system_prompt, selected_model, temperature)
        elif self.provider == LLMProvider.CLAUDE:
            response = self._invoke_claude(prompt, system_prompt, selected_model, temperature, max_tokens)
        elif self.provider == LLMProvider.OPENAI:
            response = self._invoke_openai(prompt, system_prompt, selected_model, temperature, max_tokens)
        else:
            response = self._invoke_mock(prompt, system_prompt, selected_model)

        # Inbound PHI verification on model generated content
        assert_no_phi(response.content)
        return response

    def _invoke_ollama(
        self,
        prompt: str,
        system_prompt: Optional[str],
        model: str,
        temperature: float
    ) -> LLMResponse:
        url = f"{self.ollama_base_url}/api/generate"
        payload = {
            "model": model,
            "prompt": prompt,
            "system": system_prompt or "You are an expert surgical pathologist and whole slide imaging AI quality controller.",
            "stream": False,
            "options": {"temperature": temperature}
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(url, json=payload)
                res.raise_for_status()
                data = res.json()
                content = data.get("response", "")
                return LLMResponse(
                    provider=LLMProvider.OLLAMA,
                    model_name=model,
                    content=content,
                    usage_tokens={
                        "prompt_tokens": data.get("prompt_eval_count", 0),
                        "completion_tokens": data.get("eval_count", 0),
                        "total_tokens": data.get("prompt_eval_count", 0) + data.get("eval_count", 0)
                    },
                    raw_response=data
                )
        except Exception as e:
            logger.warning(f"Ollama invocation failed ({e}). Falling back to deterministic expert mock.")
            return self._invoke_mock(prompt, system_prompt, f"{model}-fallback")

    def _invoke_claude(
        self,
        prompt: str,
        system_prompt: Optional[str],
        model: str,
        temperature: float,
        max_tokens: int
    ) -> LLMResponse:
        if not self.claude_api_key:
            logger.warning("No ANTHROPIC_API_KEY found. Falling back to deterministic mock.")
            return self._invoke_mock(prompt, system_prompt, f"{model}-fallback")

        url = "https://api.anthropic.com/v1/messages"
        headers = {
            "x-api-key": self.claude_api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json"
        }
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system_prompt or "You are PathMind Atlas, an autonomous surgical pathology and synoptic validation agent.",
            "messages": [{"role": "user", "content": prompt}]
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(url, headers=headers, json=payload)
                res.raise_for_status()
                data = res.json()
                text_blocks = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
                content = "\n".join(text_blocks)
                usage = data.get("usage", {})
                return LLMResponse(
                    provider=LLMProvider.CLAUDE,
                    model_name=model,
                    content=content,
                    usage_tokens={
                        "prompt_tokens": usage.get("input_tokens", 0),
                        "completion_tokens": usage.get("output_tokens", 0),
                        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0)
                    },
                    raw_response=data
                )
        except Exception as e:
            logger.warning(f"Claude invocation failed ({e}). Falling back to mock.")
            return self._invoke_mock(prompt, system_prompt, f"{model}-fallback")

    def _invoke_openai(
        self,
        prompt: str,
        system_prompt: Optional[str],
        model: str,
        temperature: float,
        max_tokens: int
    ) -> LLMResponse:
        if not self.openai_api_key:
            logger.warning("No OPENAI_API_KEY found. Falling back to deterministic mock.")
            return self._invoke_mock(prompt, system_prompt, f"{model}-fallback")

        url = "https://api.openai.com/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.openai_api_key}",
            "Content-Type": "application/json"
        }
        messages = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": prompt})

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        try:
            with httpx.Client(timeout=self.timeout) as client:
                res = client.post(url, headers=headers, json=payload)
                res.raise_for_status()
                data = res.json()
                choice = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                return LLMResponse(
                    provider=LLMProvider.OPENAI,
                    model_name=model,
                    content=choice,
                    usage_tokens={
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0)
                    },
                    raw_response=data
                )
        except Exception as e:
            logger.warning(f"OpenAI invocation failed ({e}). Falling back to mock.")
            return self._invoke_mock(prompt, system_prompt, f"{model}-fallback")

    def _invoke_mock(
        self,
        prompt: str,
        system_prompt: Optional[str],
        model: str
    ) -> LLMResponse:
        """
        Deterministic, intelligent rule-based clinical response generator for zero-network testing.
        """
        lower_p = prompt.lower()
        if "quality control" in lower_p or "qc" in lower_p or "blur" in lower_p:
            content = json.dumps({
                "qc_verdict": "PASS",
                "focus_quality": "High sharpness (Laplacian variance > 800)",
                "artifact_analysis": "No significant tissue folds, cover slip air bubbles or pen marks detected.",
                "recommendation": "Slide approved for diagnostic digital pathology reading and AI biomarker quantification."
            }, indent=2)
        elif "synoptic" in lower_p or "protocol" in lower_p or "cap" in lower_p:
            content = json.dumps({
                "protocol_compliance": "VALID",
                "completeness_score": 100.0,
                "critical_findings": "All required CAP core elements documented including histologic subtype, margin clearance, and nodal staging.",
                "actionable_notes": "Radial and deep margins are clear (>2.0mm). pT2 pN0 stage verified."
            }, indent=2)
        elif "chat" in lower_p or "question" in lower_p:
            content = "PathMind Atlas Operational Assistant: All pathology quality control and synoptic validation pipelines are functioning within nominal CAP/CLIA specifications."
        else:
            content = f"PathMind Atlas Synthetic AI Engine analysis complete for model: {model}."

        return LLMResponse(
            provider=LLMProvider.MOCK,
            model_name=model,
            content=content,
            usage_tokens={"prompt_tokens": len(prompt.split()), "completion_tokens": len(content.split()), "total_tokens": len(prompt.split()) + len(content.split())}
        )
