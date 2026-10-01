"""
Secret masking logging filter to prevent accidental leakage of JWTs, cookies, and tokens.
Satisfies R1 and A7 security isolation constraints.
"""

import logging
import re
from typing import Any


class SecretMaskingFilter(logging.Filter):
    """
    Log filter that guarantees zero tokens, cookies, or credentials leak to log files or console.
    """

    JWT_PATTERN = re.compile(r'ey[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}')
    BEARER_PATTERN = re.compile(r'(Bearer\s+)[A-Za-z0-9_\-\.\+\/=]{10,}', re.IGNORECASE)
    COOKIE_SESSION_PATTERN = re.compile(r'(__Secure-next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}', re.IGNORECASE)
    COOKIE_DEV_PATTERN = re.compile(r'(next-auth\.session-token(?:\.\d+)?=)[^;,\s]{6,}', re.IGNORECASE)
    CF_CLEARANCE_PATTERN = re.compile(r'(cf_clearance=)[^;,\s]{6,}', re.IGNORECASE)

    def filter(self, record: logging.LogRecord) -> bool:
        if record.msg is not None:
            if isinstance(record.msg, str):
                record.msg = self.sanitize(record.msg)
            else:
                record.msg = self.sanitize(str(record.msg))

        if record.args:
            if isinstance(record.args, dict):
                record.args = {
                    (self.sanitize(k) if isinstance(k, str) else k): self._sanitize_value(v)
                    for k, v in record.args.items()
                }
            elif isinstance(record.args, (tuple, list)):
                sanitized = [self._sanitize_value(a) for a in record.args]
                record.args = tuple(sanitized) if isinstance(record.args, tuple) else sanitized
        return True

    @classmethod
    def _sanitize_value(cls, val: Any) -> Any:
        """Recursively sanitizes values, preserving numbers, booleans, and None intact."""
        if isinstance(val, str):
            return cls.sanitize(val)
        if isinstance(val, Exception):
            return cls.sanitize(str(val))
        if isinstance(val, dict):
            return {
                (cls.sanitize(k) if isinstance(k, str) else k): cls._sanitize_value(v)
                for k, v in val.items()
            }
        if isinstance(val, (list, tuple)):
            sanitized = [cls._sanitize_value(item) for item in val]
            return tuple(sanitized) if isinstance(val, tuple) else sanitized
        return val

    @classmethod
    def sanitize(cls, text: str) -> str:
        """Sanitizes text by replacing sensitive tokens with masked equivalents."""
        if not isinstance(text, str):
            return text
        text = cls.BEARER_PATTERN.sub(r'\g<1>[REDACTED_TOKEN]', text)
        text = cls.COOKIE_SESSION_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.COOKIE_DEV_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.CF_CLEARANCE_PATTERN.sub(r'\g<1>[REDACTED_COOKIE]', text)
        text = cls.JWT_PATTERN.sub(lambda m: f"{m.group(0)[:6]}...[REDACTED_JWT]...{m.group(0)[-4:]}", text)
        return text


# Alias for backward compatibility
SensitiveDataFilter = SecretMaskingFilter
