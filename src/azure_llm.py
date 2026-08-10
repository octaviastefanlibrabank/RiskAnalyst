"""
Thin wrapper around Azure OpenAI (GPT-5-mini) so the rest of the app never
touches the OpenAI SDK directly. All other modules only depend on:

    AzureLLM.extract_structured(system_prompt, user_prompt, response_model) -> BaseModel

Credentials come from environment variables (see .env.example), never hardcoded.

Uses JSON-mode (response_format={"type": "json_object"}) rather than strict
"json_schema" structured outputs: it is the variant most consistently supported
across Azure OpenAI API versions/deployments, keeping this wrapper simple. The
target schema is instead embedded in the prompt and enforced by validating the
response against the Pydantic model, with one retry that feeds the validation
error back to the model.
"""
from __future__ import annotations

import json
import os
from typing import Type, TypeVar

from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

load_dotenv()

T = TypeVar("T", bound=BaseModel)


class LLMNotConfigured(RuntimeError):
    """Raised when required Azure OpenAI environment variables are missing."""


class AzureLLM:
    def __init__(self):
        self.api_key = os.getenv("AZURE_OPENAI_API_KEY")
        self.endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
        self.deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "gpt-5-mini")
        self.api_version = os.getenv("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")

        if not self.api_key or not self.endpoint:
            raise LLMNotConfigured(
                "AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT not set. "
                "Copy .env.example to .env and fill in your Azure OpenAI credentials, "
                "or run with --no-llm to test the parsing/KO pipeline without calling the API."
            )

        # Two Azure OpenAI calling conventions exist depending on how the resource is set up:
        #  - "classic": AzureOpenAI(azure_endpoint=..., api_version=...), endpoint like
        #    https://<resource>.openai.azure.com/
        #  - "v1 compatibility" (newer): plain OpenAI(base_url=..., api_key=...), endpoint
        #    already includes the /openai/v1 suffix, e.g.
        #    https://<resource>.openai.azure.com/openai/v1
        # We pick based on the endpoint shape - this keeps the rest of the app decoupled
        # from the distinction (see module docstring).
        from openai import AzureOpenAI, OpenAI  # imported lazily so --no-llm never requires the package config

        if "/openai/v1" in self.endpoint:
            self._client = OpenAI(api_key=self.api_key, base_url=self.endpoint)
        else:
            self._client = AzureOpenAI(
                api_key=self.api_key,
                azure_endpoint=self.endpoint,
                api_version=self.api_version,
            )

    def extract_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        response_model: Type[T],
        max_retries: int = 2,
    ) -> T:
        schema = response_model.model_json_schema()
        full_system = (
            f"{system_prompt}\n\n"
            "Raspunde STRICT cu un obiect JSON valid care respecta urmatoarea schema "
            "JSON Schema (respecta numele campurilor exact; foloseste null pentru "
            "orice informatie care nu se regaseste clar in documentele furnizate; "
            "nu adauga alte chei):\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )

        messages = [
            {"role": "system", "content": full_system},
            {"role": "user", "content": user_prompt},
        ]

        last_error: Exception | None = None
        for attempt in range(max_retries + 1):
            content = self._chat(messages)
            try:
                data = json.loads(content)
                return response_model.model_validate(data)
            except (json.JSONDecodeError, ValidationError) as e:
                last_error = e
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "Raspunsul anterior nu este JSON valid conform schemei. "
                            f"Eroare: {e}\nTe rog raspunde din nou STRICT cu JSON valid, fara text suplimentar."
                        ),
                    }
                )
        raise RuntimeError(f"LLM did not return valid structured output after {max_retries + 1} attempts: {last_error}")

    def _chat(self, messages: list[dict]) -> str:
        response = self._client.chat.completions.create(
            model=self.deployment,
            messages=messages,
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""
