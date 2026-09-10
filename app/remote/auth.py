"""Worker authentication contract.

Production deployments plug a real verifier (HMAC, JWT, mTLS
identity binding) behind TokenAuthenticator. The pool only calls
verify(); tokens are never logged.
"""

from __future__ import annotations

import hmac
from abc import ABC, abstractmethod


class TokenAuthenticator(ABC):
    @abstractmethod
    def verify(self, token: str, worker_id: str) -> bool:
        raise NotImplementedError


class AllowAllAuthenticator(TokenAuthenticator):
    def verify(self, token: str, worker_id: str) -> bool:
        return True


class StaticTokenAuthenticator(TokenAuthenticator):
    """Shared-secret placeholder for tests and local dev."""

    def __init__(self, expected_token: str):
        self._expected = expected_token

    def verify(self, token: str, worker_id: str) -> bool:
        if not worker_id:
            return False
        return hmac.compare_digest(str(token), self._expected)
