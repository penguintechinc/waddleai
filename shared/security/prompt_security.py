"""Prompt Security & Injection Detection System.

Detects and prevents prompt injection attacks and malicious content.
"""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class ThreatType(Enum):
    """Types of security threats."""

    PROMPT_INJECTION = "prompt_injection"
    JAILBREAK = "jailbreak"
    DATA_EXTRACTION = "data_extraction"
    SYSTEM_PROMPT_LEAK = "system_prompt_leak"
    CREDENTIAL_HARVESTING = "credential_harvesting"


class Severity(Enum):
    """Threat severity levels."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class Action(Enum):
    """Security response actions."""

    LOG = "log"
    SANITIZE = "sanitize"
    BLOCK = "block"
    RATE_LIMIT = "rate_limit"


@dataclass
class ThreatDetection:
    """Result of threat detection."""

    threat_type: ThreatType
    severity: Severity
    confidence: float
    matched_patterns: list[str]
    description: str
    suggested_action: Action


@dataclass
class SecurityPolicy:
    """Security policy configuration."""

    name: str
    enabled: bool
    max_prompt_length: int
    suspicious_pattern_threshold: int
    actions: dict[ThreatType, Action]
    rate_limit_threshold: int


class PromptSecurityScanner:
    """Comprehensive prompt security scanner."""

    # Threat detection patterns
    THREAT_PATTERNS = {
        ThreatType.PROMPT_INJECTION: [
            r"ignore\s+(?:previous|all|above|prior)\s+(?:instructions?|prompts?|rules?|directions?)",
            r"forget\s+(?:previous|all|above|prior)\s+(?:instructions?|prompts?|rules?|directions?)",
            r"system\s*:\s*(?:you\s+are\s+now|new\s+instructions?)",
            r"</\s*(?:instruction|prompt|system)\s*>.*<\s*(?:instruction|prompt|system)\s*>",
            r"---\s*end\s+of\s+(?:prompt|instructions?|system)\s*---",
            r"new\s+(?:instructions?|prompt|system|role)\s*:",
            r"override\s+(?:previous|all|system)\s+(?:instructions?|rules?)",
            r"disregard\s+(?:previous|all|system)\s+(?:instructions?|rules?)",
            r"replace\s+(?:previous|all|system)\s+(?:instructions?|rules?)",
            r"instead\s+of\s+following\s+(?:instructions?|rules?)",
            # §8.5.4 spoof-as-threat: gaming the guard filter is itself a
            # threat signal (raise suspicion, never lower it). Covers guard-
            # verdict-token injection (telling the guard what to answer),
            # ShieldGemma/Granite-Guardian prompt-format delimiter escapes,
            # and explicit filter-override phrasing.
            r"(?:respond|answer|reply|say)\s+(?:with\s+)?(?:exactly\s+)?['\"]?"
            r"(?:yes|no|safe|allowed?|block|no\s*violation)['\"]?\s*(?:only|exactly)?",
            r"<start_of_turn>|<end_of_turn>",
            r"</?content>",
            r"you\s+are\s+now\s+unfiltered",
        ],
        ThreatType.JAILBREAK: [
            r"pretend\s+(?:you\s+are|to\s+be)\s+(?:a\s+)?(?:different|another|\w+)",
            r"roleplay\s+(?:as|being)\s+(?:a\s+)?(?:\w+)",
            r"simulate\s+(?:a|being)\s+(?:\w+)",
            r"act\s+(?:as\s+if|like)\s+you\s+(?:are|were)\s+(?:a\s+)?(?:\w+)",
            r"bypass\s+(?:your|the)\s+(?:safety|guidelines|restrictions|limitations)",
            r"break\s+(?:your|the)\s+(?:rules|guidelines|restrictions)",
            r"violate\s+(?:your|the)\s+(?:rules|guidelines|restrictions)",
            r"go\s+against\s+(?:your|the)\s+(?:programming|instructions)",
            r"you\s+(?:can|are able to|should)\s+(?:do|say|ignore)\s+anything",
            r"there\s+are\s+no\s+(?:rules|restrictions|limitations)",
            r"hypothetically\s+speaking",
            r"in\s+a\s+(?:fictional|hypothetical)\s+scenario",
        ],
        ThreatType.DATA_EXTRACTION: [
            r"(?:show|tell|give|reveal)\s+me\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions?)",
            r"what\s+(?:are\s+)?your\s+(?:initial\s+)?(?:instructions?|directives?|prompt)",
            r"reveal\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)",
            r"display\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)",
            r"print\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)",
            r"output\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)",
            r"what\s+(?:is|was)\s+your\s+(?:original\s+)?(?:prompt|instructions?)",
            r"copy\s+and\s+paste\s+your\s+(?:prompt|instructions?)",
        ],
        ThreatType.SYSTEM_PROMPT_LEAK: [
            r"<\|(?:im_start|im_end)\|>",
            r"<\|system\|>",
            r"<\|user\|>",
            r"<\|assistant\|>",
            r"###\s+(?:System|Instruction|Human|Assistant)",
            r"\[INST\].*\[/INST\]",
            r"<s>.*</s>",
            r"{{.*}}",
        ],
        ThreatType.CREDENTIAL_HARVESTING: [
            r'(?:api\s+key|api_key|apikey)\s*[:=]\s*["\']?[\w\-]{20,}',
            r'(?:password|passwd|pwd)\s*[:=]\s*["\']?\w{6,}',
            r'(?:token|access_token|auth_token)\s*[:=]\s*["\']?[\w\-]{20,}',
            r'(?:secret|client_secret|api_secret)\s*[:=]\s*["\']?[\w\-]{20,}',
            r'(?:username|user|login)\s*[:=]\s*["\']?\w{3,}',
            r"sk-[a-zA-Z0-9]{20,}",  # OpenAI API key pattern
            r"xoxb-[a-zA-Z0-9\-]{10,}",  # Slack token pattern
            r"anthropic-[a-zA-Z0-9]{20,}",  # Anthropic API key pattern
        ],
    }

    # Security policies
    SECURITY_POLICIES = {
        "strict": SecurityPolicy(
            name="strict",
            enabled=True,
            max_prompt_length=10000,
            suspicious_pattern_threshold=1,
            actions={
                ThreatType.PROMPT_INJECTION: Action.BLOCK,
                ThreatType.JAILBREAK: Action.BLOCK,
                ThreatType.DATA_EXTRACTION: Action.BLOCK,
                ThreatType.SYSTEM_PROMPT_LEAK: Action.BLOCK,
                ThreatType.CREDENTIAL_HARVESTING: Action.BLOCK,
            },
            rate_limit_threshold=10,
        ),
        "balanced": SecurityPolicy(
            name="balanced",
            enabled=True,
            max_prompt_length=50000,
            suspicious_pattern_threshold=2,
            actions={
                ThreatType.PROMPT_INJECTION: Action.BLOCK,
                ThreatType.JAILBREAK: Action.SANITIZE,
                ThreatType.DATA_EXTRACTION: Action.BLOCK,
                ThreatType.SYSTEM_PROMPT_LEAK: Action.SANITIZE,
                ThreatType.CREDENTIAL_HARVESTING: Action.BLOCK,
            },
            rate_limit_threshold=20,
        ),
        "permissive": SecurityPolicy(
            name="permissive",
            enabled=True,
            max_prompt_length=100000,
            suspicious_pattern_threshold=3,
            actions={
                ThreatType.PROMPT_INJECTION: Action.SANITIZE,
                ThreatType.JAILBREAK: Action.LOG,
                ThreatType.DATA_EXTRACTION: Action.SANITIZE,
                ThreatType.SYSTEM_PROMPT_LEAK: Action.LOG,
                ThreatType.CREDENTIAL_HARVESTING: Action.BLOCK,
            },
            rate_limit_threshold=50,
        ),
    }

    def __init__(self, db, policy_name: str = "balanced"):
        """Bind the DAL handle and compile the regex patterns for the named security policy."""
        self.db = db
        self.policy = self.SECURITY_POLICIES.get(policy_name, self.SECURITY_POLICIES["balanced"])

        # Compile regex patterns for performance
        self.compiled_patterns = {}
        for threat_type, patterns in self.THREAT_PATTERNS.items():
            self.compiled_patterns[threat_type] = [
                re.compile(pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL)
                for pattern in patterns
            ]

    def scan_prompt(
        self,
        prompt: str,
        user_id: int | None = None,
        api_key_id: int | None = None,
        ip_address: str | None = None,
    ) -> tuple[list[ThreatDetection], str]:
        """Scan prompt for security threats.

        Returns (detected_threats, sanitized_prompt).
        """
        if not self.policy.enabled:
            return [], prompt

        # Check prompt length
        if len(prompt) > self.policy.max_prompt_length:
            threat = ThreatDetection(
                threat_type=ThreatType.PROMPT_INJECTION,
                severity=Severity.MEDIUM,
                confidence=1.0,
                matched_patterns=["prompt_too_long"],
                description=(
                    f"Prompt exceeds maximum length of {self.policy.max_prompt_length} characters"
                ),
                suggested_action=Action.BLOCK,
            )
            self._log_threat(threat, prompt, user_id, api_key_id, ip_address)
            return [threat], prompt

        detected_threats = []
        sanitized_prompt = prompt

        # Pattern-based detection
        for threat_type, patterns in self.compiled_patterns.items():
            matches = []
            for pattern in patterns:
                found_matches = pattern.findall(prompt)
                if found_matches:
                    matches.extend(
                        [
                            str(match) if isinstance(match, str) else str(match[0])
                            for match in found_matches
                        ]
                    )

            if len(matches) >= self.policy.suspicious_pattern_threshold:
                confidence = min(1.0, len(matches) / 5.0)  # Scale confidence
                severity = self._calculate_severity(threat_type, len(matches))

                threat = ThreatDetection(
                    threat_type=threat_type,
                    severity=severity,
                    confidence=confidence,
                    matched_patterns=matches[:5],  # Limit to first 5 matches
                    description=f"Detected {threat_type.value} patterns: {len(matches)} matches",
                    suggested_action=self.policy.actions.get(threat_type, Action.LOG),
                )

                detected_threats.append(threat)

                # Apply sanitization if needed
                if threat.suggested_action == Action.SANITIZE:
                    sanitized_prompt = self._sanitize_prompt(
                        sanitized_prompt, threat_type, patterns
                    )

        # Log threats
        for threat in detected_threats:
            self._log_threat(threat, prompt, user_id, api_key_id, ip_address)

        return detected_threats, sanitized_prompt

    def scan_messages(
        self,
        messages: list[dict[str, str]],
        user_id: int | None = None,
        api_key_id: int | None = None,
        ip_address: str | None = None,
    ) -> tuple[list[ThreatDetection], list[dict[str, str]]]:
        """Scan a list of messages for security threats.

        Returns (all_threats, sanitized_messages).
        """
        all_threats = []
        sanitized_messages = []

        for message in messages:
            content = message.get("content", "")
            threats, sanitized = self.scan_prompt(content, user_id, api_key_id, ip_address)

            all_threats.extend(threats)
            sanitized_messages.append({**message, "content": sanitized})

        return all_threats, sanitized_messages

    def should_block(self, threats: list[ThreatDetection]) -> bool:
        """Check if any threat requires blocking."""
        return any(t.suggested_action == Action.BLOCK for t in threats)

    def _calculate_severity(self, threat_type: ThreatType, match_count: int) -> Severity:
        """Calculate threat severity based on type and match count."""
        base_severity = {
            ThreatType.PROMPT_INJECTION: Severity.HIGH,
            ThreatType.JAILBREAK: Severity.MEDIUM,
            ThreatType.DATA_EXTRACTION: Severity.HIGH,
            ThreatType.SYSTEM_PROMPT_LEAK: Severity.CRITICAL,
            ThreatType.CREDENTIAL_HARVESTING: Severity.CRITICAL,
        }.get(threat_type, Severity.LOW)

        # Escalate severity based on match count
        if match_count >= 5:
            if base_severity == Severity.LOW:
                return Severity.MEDIUM
            elif base_severity == Severity.MEDIUM:
                return Severity.HIGH
            elif base_severity == Severity.HIGH:
                return Severity.CRITICAL

        return base_severity

    def _sanitize_prompt(
        self, prompt: str, threat_type: ThreatType, patterns: list[re.Pattern]
    ) -> str:
        """Sanitize prompt by removing or modifying threatening content."""
        sanitized = prompt

        for pattern in patterns:
            # Replace matches with sanitized versions
            if threat_type == ThreatType.PROMPT_INJECTION:
                sanitized = pattern.sub("[REDACTED: Instruction override attempt]", sanitized)
            elif threat_type == ThreatType.JAILBREAK:
                sanitized = pattern.sub("[REDACTED: Roleplay attempt]", sanitized)
            elif threat_type == ThreatType.DATA_EXTRACTION:
                sanitized = pattern.sub("[REDACTED: System information request]", sanitized)
            elif threat_type == ThreatType.SYSTEM_PROMPT_LEAK:
                sanitized = pattern.sub("[REDACTED: System token]", sanitized)
            elif threat_type == ThreatType.CREDENTIAL_HARVESTING:
                sanitized = pattern.sub("[REDACTED: Credential]", sanitized)

        return sanitized

    def _log_threat(
        self,
        threat: ThreatDetection,
        original_prompt: str,
        user_id: int | None = None,
        api_key_id: int | None = None,
        ip_address: str | None = None,
    ):
        """Log security threat to database."""
        try:
            # Get organization ID if we have user or API key
            org_id = None
            if api_key_id:
                api_key = self.db(self.db.api_keys.id == api_key_id).select().first()
                if api_key:
                    org_id = api_key.organization_id
                    if not user_id:
                        user_id = api_key.user_id

            if user_id and not org_id:
                user = self.db(self.db.users.id == user_id).select().first()
                if user:
                    org_id = user.organization_id

            # Create prompt sample (truncated for storage)
            prompt_sample = original_prompt[:1000] if original_prompt else ""

            # Generate request hash
            request_hash = hashlib.md5(
                (original_prompt + str(datetime.utcnow().timestamp())).encode(),
                usedforsecurity=False,
            ).hexdigest()

            # Log to database
            self.db.security_logs.insert(
                timestamp=datetime.utcnow(),
                api_key_id=api_key_id,
                user_id=user_id,
                organization_id=org_id,
                request_hash=request_hash,
                threat_type=threat.threat_type.value,
                severity=threat.severity.value,
                blocked=(threat.suggested_action == Action.BLOCK),
                prompt_sample=prompt_sample,
                detection_rules=json.dumps(
                    {
                        "patterns": threat.matched_patterns,
                        "confidence": threat.confidence,
                        "policy": self.policy.name,
                    }
                ),
                ip_address=ip_address,
            )

            # Log to application logger
            logger.warning(
                f"Security threat detected: {threat.threat_type.value} "
                f"(severity: {threat.severity.value}, confidence: {threat.confidence:.2f}) "
                f"User: {user_id}, API Key: {api_key_id}, IP: {ip_address}"
            )

        except Exception as e:
            logger.error(f"Failed to log security threat: {e}")

    def set_policy(self, policy_name: str) -> bool:
        """Change the security policy."""
        if policy_name in self.SECURITY_POLICIES:
            self.policy = self.SECURITY_POLICIES[policy_name]
            logger.info(f"Security policy changed to: {policy_name}")
            return True
        return False

    def add_custom_pattern(self, threat_type: ThreatType, pattern: str) -> bool:
        """Add a custom detection pattern."""
        try:
            compiled = re.compile(pattern, re.IGNORECASE | re.MULTILINE | re.DOTALL)
            if threat_type not in self.compiled_patterns:
                self.compiled_patterns[threat_type] = []
            self.compiled_patterns[threat_type].append(compiled)
            logger.info(f"Added custom pattern for {threat_type.value}")
            return True
        except re.error as e:
            logger.error(f"Invalid regex pattern: {e}")
            return False

    def check_rate_limit(
        self,
        user_id: int | None = None,
        api_key_id: int | None = None,
        ip_address: str | None = None,
    ) -> bool:
        """Check if user/IP has exceeded threat rate limit."""
        if not self.policy.enabled:
            return True

        # Check threats in the last hour
        one_hour_ago = datetime.utcnow() - timedelta(hours=1)

        conditions = [self.db.security_logs.timestamp > one_hour_ago]

        if api_key_id:
            conditions.append(self.db.security_logs.api_key_id == api_key_id)
        elif user_id:
            conditions.append(self.db.security_logs.user_id == user_id)
        elif ip_address:
            conditions.append(self.db.security_logs.ip_address == ip_address)
        else:
            return True  # No identifier to check

        threat_count = self.db(conditions[0]).count()
        for condition in conditions[1:]:
            threat_count = self.db(threat_count & condition).count()

        return threat_count < self.policy.rate_limit_threshold

    def get_security_stats(self, hours: int = 24) -> dict[str, Any]:
        """Get security statistics for the specified time period."""
        since = datetime.utcnow() - timedelta(hours=hours)

        logs = self.db(self.db.security_logs.timestamp > since).select()

        stats: dict[str, Any] = {
            "total_threats": len(logs),
            "blocked_requests": len([log for log in logs if log.blocked]),
            "threat_types": {},
            "severity_breakdown": {},
            "top_ips": {},
            "top_users": {},
        }

        for log in logs:
            # Count by threat type
            threat_type = log.threat_type
            stats["threat_types"][threat_type] = stats["threat_types"].get(threat_type, 0) + 1

            # Count by severity
            severity = log.severity
            stats["severity_breakdown"][severity] = stats["severity_breakdown"].get(severity, 0) + 1

            # Count by IP
            if log.ip_address:
                stats["top_ips"][log.ip_address] = stats["top_ips"].get(log.ip_address, 0) + 1

            # Count by user
            if log.user_id:
                stats["top_users"][log.user_id] = stats["top_users"].get(log.user_id, 0) + 1

        return stats


def create_security_scanner(db, policy: str = "balanced") -> PromptSecurityScanner:
    """Factory function to create security scanner."""
    return PromptSecurityScanner(db, policy)
