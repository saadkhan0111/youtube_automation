"""OpenAI calls for the Studio. Reuses the clipper's OpenAI client; every call reports its usage to costs.py.

Test hooks (module-level) let tests run without any network access.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

from ..clipper import ai as clip_ai
from . import costs

log = logging.getLogger("ytshorts.studio.llm")

# Test hooks
web_research_hook = None   # fn(model, prompt) -> WebResult
chat_json_hook = None      # fn(model, system, user, schema) -> dict


@dataclass
class WebResult:
    text: str
    visited_urls: list[str] = field(default_factory=list)   # every URL the search tool opened or cited
    search_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0


def web_research(model: str, prompt: str, project_id: int | None = None, stage: str = "research") -> WebResult:
    """Run a research prompt with the web_search tool and collect every source URL it actually used."""
    if web_research_hook is not None:
        res = web_research_hook(model, prompt)
    else:
        r = clip_ai.client().responses.create(model=model, tools=[{"type": "web_search"}], input=prompt,
                                              include=["web_search_call.action.sources"])
        urls, calls = [], 0
        for item in r.output:
            if item.type == "web_search_call":
                calls += 1
                for s in (getattr(getattr(item, "action", None), "sources", None) or []):
                    if getattr(s, "url", None):
                        urls.append(s.url)
            elif item.type == "message":
                for part in item.content:
                    for a in (getattr(part, "annotations", None) or []):
                        if getattr(a, "type", "") == "url_citation" and getattr(a, "url", None):
                            urls.append(a.url)
        res = WebResult(r.output_text or "", urls, calls, r.usage.input_tokens, r.usage.output_tokens)
    costs.record(project_id, stage, "openai", "web_search",
                 {"input_tokens": res.input_tokens, "output_tokens": res.output_tokens, "calls": res.search_calls},
                 model=model)
    return res


def chat_json(model: str, system: str, user: str, schema: dict, name: str = "result",
              project_id: int | None = None, stage: str | None = None) -> dict:
    """Structured JSON output (strict schema). Raises on refusal/truncation/invalid JSON."""
    if chat_json_hook is not None:
        return chat_json_hook(model, system, user, schema)
    r = clip_ai.client().chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        response_format={"type": "json_schema", "json_schema": {"name": name, "schema": schema, "strict": True}},
    )
    costs.record(project_id, stage, "openai", "chat",
                 {"input_tokens": r.usage.prompt_tokens, "output_tokens": r.usage.completion_tokens}, model=model)
    choice = r.choices[0]
    if getattr(choice.message, "refusal", None):
        raise RuntimeError(f"Model refused: {choice.message.refusal}")
    if choice.finish_reason == "length":
        raise RuntimeError("Model response was truncated.")
    return json.loads(choice.message.content)
