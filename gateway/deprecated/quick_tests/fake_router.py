"""A fake router whose behaviour depends on a word in the prompt, so tests can force each case."""

import asyncio

from gateway.plugins.router import RouteDecision, RouteRequest

CALLS: list[RouteRequest] = []   # every request the fake router received, so tests can count calls


class FakeRouter:
    """Picks mock/echo with level=high, unless the prompt contains a trigger word:
    keep -> no opinion, bogus -> a model that does not exist, boom -> crashes, slow -> times out."""

    async def route(self, req: RouteRequest) -> RouteDecision | None:
        """Record the call, then act on the trigger word in the prompt (see the class docstring)."""
        CALLS.append(req)
        if "keep" in req.prompt:
            return None
        if "bogus" in req.prompt:
            return RouteDecision(provider="mock", model="does-not-exist")
        if "boom" in req.prompt:
            raise RuntimeError("router exploded")
        if "slow" in req.prompt:
            await asyncio.sleep(5)
        return RouteDecision(provider="mock", model="echo", params={"level": "high"}, reason="test rule", score=0.9)
