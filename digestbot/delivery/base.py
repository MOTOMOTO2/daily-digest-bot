"""Channel interface and the HTTP posting helper shared by webhook channels."""

from __future__ import annotations

from typing import Any

import httpx

from ..config import AlertConfig, DeliveryConfig


class DeliveryError(Exception):
    """Delivery failed. Treated as a hard failure by the pipeline."""

    def __init__(self, message: str, *, retryable: bool = True, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class Channel:
    """A destination the digest can be posted to."""

    kind: str = "base"

    def __init__(self, config: DeliveryConfig | AlertConfig) -> None:
        self.config = config

    @property
    def label(self) -> str:
        return self.config.channel

    def send(self, subject: str, markdown: str, *, client: httpx.Client | None = None) -> dict[str, Any]:
        """Deliver the digest, or raise DeliveryError. Returns a receipt."""
        raise NotImplementedError

    def describe(self) -> str:
        return f"{self.kind}:{self.label}"


def post_json(
    url: str,
    payload: dict[str, Any],
    *,
    timeout: float,
    client: httpx.Client | None = None,
    expect_json: bool = False,
) -> dict[str, Any]:
    """POST JSON to a webhook, mapping every failure to DeliveryError."""
    owns_client = client is None
    http = client or httpx.Client(follow_redirects=True)
    try:
        response = http.post(url, json=payload, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise DeliveryError(f"timed out after {timeout:g}s posting to the webhook", retryable=True) from exc
    except httpx.TransportError as exc:
        raise DeliveryError(f"network error posting to the webhook: {exc}", retryable=True) from exc
    except httpx.HTTPError as exc:
        raise DeliveryError(f"HTTP error posting to the webhook: {exc}", retryable=True) from exc
    finally:
        if owns_client:
            http.close()

    body = response.text[:500].replace("\n", " ").strip()
    if response.status_code >= 400:
        # 4xx other than 429 means the request itself is wrong (revoked
        # webhook, malformed payload); retrying unchanged will not help.
        retryable = response.status_code >= 500 or response.status_code == 429
        raise DeliveryError(
            f"webhook returned HTTP {response.status_code}" + (f": {body}" if body else ""),
            retryable=retryable,
            status_code=response.status_code,
        )

    receipt: dict[str, Any] = {"status_code": response.status_code, "response": body or None}
    if expect_json:
        try:
            receipt["json"] = response.json()
        except ValueError:
            receipt["json"] = None
    return receipt
