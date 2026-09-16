"""Anti-corruption port for external Project delivery implementations."""

from __future__ import annotations

from typing import Protocol

from .models import DeliveryRequest, DeliveryResult


class DeliveryProvider(Protocol):
    """Provider-neutral delivery adapter contract.

    Adapters receive a durable ``operation_id`` and are expected to use it as an
    idempotency key where the external system supports one. ``reconcile`` must
    observe a previously requested operation without creating a new external
    side effect; returning ``None`` means the outcome cannot currently be
    established.
    """

    @property
    def provider_id(self) -> str:
        ...

    def deliver(self, request: DeliveryRequest) -> DeliveryResult:
        ...

    def reconcile(self, request: DeliveryRequest) -> DeliveryResult | None:
        ...
