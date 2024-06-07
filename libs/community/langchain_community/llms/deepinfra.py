import json
from typing import Any, AsyncIterator, Dict, Iterator, List, Mapping, Optional

import aiohttp
from langchain_core.callbacks import (
    AsyncCallbackManagerForLLMRun,
    CallbackManagerForLLMRun,
)
from langchain_core.language_models.llms import LLM
from langchain_core.outputs import GenerationChunk
from langchain_core.pydantic_v1 import Extra, root_validator
from langchain_core.utils import get_from_dict_or_env

from langchain_community.utilities.requests import Requests

DEFAULT_MODEL_ID = "meta-llama/Meta-Llama-3-70B-Instruct"


class DeepInfra(LLM):
    """DeepInfra models.

    To use, you should have the environment variable ``DEEPINFRA_API_TOKEN``
    set with your API token, or pass it as a named parameter to the
    constructor.

    Only supports `text-generation` and `text2text-generation` for now.

    Example:
        .. code-block:: python

            from langchain_community.llms import DeepInfra
            di = DeepInfra(model_id="google/flan-t5-xl",
                                deepinfra_api_token="my-api-key")
    """

    model_id: str = DEFAULT_MODEL_ID
    model_kwargs: Optional[Dict] = None

    deepinfra_api_token: Optional[str] = None

    class Config:
        """Configuration for this pydantic object."""

        extra = Extra.forbid

    @root_validator()
    def validate_environment(cls, values: Dict) -> Dict:
        """Validate that api key and python package exists in environment."""
        deepinfra_api_token = get_from_dict_or_env(
            values, "deepinfra_api_token", "DEEPINFRA_API_TOKEN"
        )
        values["deepinfra_api_token"] = deepinfra_api_token
        return values

    @property
    def _identifying_params(self) -> Mapping[str, Any]:
        """Get the identifying parameters."""
        return {
            **{"model_id": self.model_id},
            **{"model_kwargs": self.model_kwargs},
        }

    @property
    def _llm_type(self) -> str:
        """Return type of llm."""
        return "deepinfra"

    def _url(self) -> str:
        return f"https://api.deepinfra.com/v1/inference/{self.model_id}"

    def _headers(self) -> Dict:
        return {
            "Authorization": f"bearer {self.deepinfra_api_token}",
            "Content-Type": "application/json",
        }

    def _body(self, prompt: str, kwargs: Any) -> Dict:
        model_kwargs = self.model_kwargs or {}
        model_kwargs = {**model_kwargs, **kwargs}

        return {
            "input": prompt,
            **model_kwargs,
        }

    def _handle_status(self, code: int, text: Any) -> None:
        if code >= 500:
            raise Exception(f"DeepInfra Server: Error {text}")
        elif code == 401:
            raise Exception(f"DeepInfra Server: Unauthorized")
        elif code == 403:
            raise Exception(f"DeepInfra Server: Unauthorized")
        elif code == 404:
            raise Exception(f"DeepInfra Server: Model not found {self.model_id}")
        elif code == 429:
            raise Exception(f"DeepInfra Server: Rate limit exceeded")
        elif code >= 400:
            raise ValueError(f"DeepInfra received an invalid payload: {text}")
        elif code != 200:
            raise Exception(
                f"DeepInfra returned an unexpected response with status "
                f"{code}: {text}"
            )

    def _call(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        """Call out to DeepInfra's inference API endpoint.

        Args:
            prompt: The prompt to pass into the model.
            stop: Optional list of stop words to use when generating.

        Returns:
            The string generated by the model.

        Example:
            .. code-block:: python

                response = di("Tell me a joke.")
        """

        request = Requests(headers=self._headers())
        response = request.post(url=self._url(), data=self._body(prompt, kwargs))

        self._handle_status(response.status_code, response.text)
        data = response.json()

        return data["results"][0]["generated_text"]

    async def _acall(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> str:
        request = Requests(headers=self._headers())
        async with request.apost(
            url=self._url(), data=self._body(prompt, kwargs)
        ) as response:
            self._handle_status(response.status, response.text)
            data = await response.json()
            return data["results"][0]["generated_text"]

    def _stream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[CallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> Iterator[GenerationChunk]:
        request = Requests(headers=self._headers())
        response = request.post(
            url=self._url(), data=self._body(prompt, {**kwargs, "stream": True})
        )
        response_text = response.text
        if "error" in response_text:
            raise Exception(f"DeepInfra Server: {response_text}")
        self._handle_status(response.status_code, response.text)
        for line in _parse_stream(response.iter_lines()):
            chunk = _handle_sse_line(line)
            if chunk:
                if run_manager:
                    run_manager.on_llm_new_token(chunk.text)
                yield chunk

    async def _astream(
        self,
        prompt: str,
        stop: Optional[List[str]] = None,
        run_manager: Optional[AsyncCallbackManagerForLLMRun] = None,
        **kwargs: Any,
    ) -> AsyncIterator[GenerationChunk]:
        request = Requests(headers=self._headers())
        async with request.apost(
            url=self._url(), data=self._body(prompt, {**kwargs, "stream": True})
        ) as response:
            response_text = await response.text()
            if "error" in response_text:
                raise Exception(f"DeepInfra Server: {response_text}")
            self._handle_status(response.status, response.text)
            async for line in _parse_stream_async(response.content):
                chunk = _handle_sse_line(line)
                if chunk:
                    if run_manager:
                        await run_manager.on_llm_new_token(chunk.text)
                    yield chunk


def _parse_stream(rbody: Iterator[bytes]) -> Iterator[str]:
    for line in rbody:
        _line = _parse_stream_helper(line)
        if _line is not None:
            yield _line


async def _parse_stream_async(rbody: aiohttp.StreamReader) -> AsyncIterator[str]:
    async for line in rbody:
        _line = _parse_stream_helper(line)
        if _line is not None:
            yield _line


def _parse_stream_helper(line: bytes) -> Optional[str]:
    if line and line.startswith(b"data:"):
        if line.startswith(b"data: "):
            # SSE event may be valid when it contain whitespace
            line = line[len(b"data: ") :]
        else:
            line = line[len(b"data:") :]
        if line.strip() == b"[DONE]":
            # return here will cause GeneratorExit exception in urllib3
            # and it will close http connection with TCP Reset
            return None
        else:
            return line.decode("utf-8")
    return None


def _handle_sse_line(line: str) -> Optional[GenerationChunk]:
    try:
        obj = json.loads(line)
        return GenerationChunk(
            text=obj.get("token", {}).get("text"),
        )
    except Exception:
        return None
