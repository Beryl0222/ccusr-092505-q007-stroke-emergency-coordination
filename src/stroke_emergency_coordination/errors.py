"""领域规则违例。code 稳定，供接入方按代码分支处理。"""

from __future__ import annotations


class DomainError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
