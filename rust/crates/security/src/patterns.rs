//! Exported THREAT_PATTERNS table (verbatim Python source patterns).
//!
//! Generated from `shared/security/prompt_security.py` via `scripts/export-patterns.py`;
//! the `patterns_match_export` test fails if this table drifts from the Python source.

use crate::prompt::ThreatType;

/// Every threat pattern as `(threat type, Python regex source)` in Python dict order.
pub const THREAT_PATTERNS: &[(ThreatType, &str)] = &[
    (
        ThreatType::PromptInjection,
        r##"ignore\s+(?:previous|all|above|prior)\s+(?:instructions?|prompts?|rules?|directions?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"forget\s+(?:previous|all|above|prior)\s+(?:instructions?|prompts?|rules?|directions?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"system\s*:\s*(?:you\s+are\s+now|new\s+instructions?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"</\s*(?:instruction|prompt|system)\s*>.*<\s*(?:instruction|prompt|system)\s*>"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"---\s*end\s+of\s+(?:prompt|instructions?|system)\s*---"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"new\s+(?:instructions?|prompt|system|role)\s*:"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"override\s+(?:previous|all|system)\s+(?:instructions?|rules?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"disregard\s+(?:previous|all|system)\s+(?:instructions?|rules?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"replace\s+(?:previous|all|system)\s+(?:instructions?|rules?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"instead\s+of\s+following\s+(?:instructions?|rules?)"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"(?:respond|answer|reply|say)\s+(?:with\s+)?(?:exactly\s+)?['\"]?(?:yes|no|safe|allowed?|block|no\s*violation)['\"]?\s*(?:only|exactly)?"##,
    ),
    (
        ThreatType::PromptInjection,
        r##"<start_of_turn>|<end_of_turn>"##,
    ),
    (ThreatType::PromptInjection, r##"</?content>"##),
    (
        ThreatType::PromptInjection,
        r##"you\s+are\s+now\s+unfiltered"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"pretend\s+(?:you\s+are|to\s+be)\s+(?:a\s+)?(?:different|another|\w+)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"roleplay\s+(?:as|being)\s+(?:a\s+)?(?:\w+)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"simulate\s+(?:a|being)\s+(?:\w+)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"act\s+(?:as\s+if|like)\s+you\s+(?:are|were)\s+(?:a\s+)?(?:\w+)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"bypass\s+(?:your|the)\s+(?:safety|guidelines|restrictions|limitations)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"break\s+(?:your|the)\s+(?:rules|guidelines|restrictions)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"violate\s+(?:your|the)\s+(?:rules|guidelines|restrictions)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"go\s+against\s+(?:your|the)\s+(?:programming|instructions)"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"you\s+(?:can|are able to|should)\s+(?:do|say|ignore)\s+anything"##,
    ),
    (
        ThreatType::Jailbreak,
        r##"there\s+are\s+no\s+(?:rules|restrictions|limitations)"##,
    ),
    (ThreatType::Jailbreak, r##"hypothetically\s+speaking"##),
    (
        ThreatType::Jailbreak,
        r##"in\s+a\s+(?:fictional|hypothetical)\s+scenario"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"(?:show|tell|give|reveal)\s+me\s+(?:your|the)\s+(?:system\s+)?(?:prompt|instructions?)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"what\s+(?:are\s+)?your\s+(?:initial\s+)?(?:instructions?|directives?|prompt)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"reveal\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"display\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"print\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"output\s+(?:your\s+)?(?:system\s+)?(?:prompt|instructions?|directives?)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"what\s+(?:is|was)\s+your\s+(?:original\s+)?(?:prompt|instructions?)"##,
    ),
    (
        ThreatType::DataExtraction,
        r##"copy\s+and\s+paste\s+your\s+(?:prompt|instructions?)"##,
    ),
    (
        ThreatType::SystemPromptLeak,
        r##"<\|(?:im_start|im_end)\|>"##,
    ),
    (ThreatType::SystemPromptLeak, r##"<\|system\|>"##),
    (ThreatType::SystemPromptLeak, r##"<\|user\|>"##),
    (ThreatType::SystemPromptLeak, r##"<\|assistant\|>"##),
    (
        ThreatType::SystemPromptLeak,
        r##"###\s+(?:System|Instruction|Human|Assistant)"##,
    ),
    (ThreatType::SystemPromptLeak, r##"\[INST\].*\[/INST\]"##),
    (ThreatType::SystemPromptLeak, r##"<s>.*</s>"##),
    (ThreatType::SystemPromptLeak, r##"{{.*}}"##),
    (
        ThreatType::CredentialHarvesting,
        r##"(?:api\s+key|api_key|apikey)\s*[:=]\s*["\']?[\w\-]{20,}"##,
    ),
    (
        ThreatType::CredentialHarvesting,
        r##"(?:password|passwd|pwd)\s*[:=]\s*["\']?\w{6,}"##,
    ),
    (
        ThreatType::CredentialHarvesting,
        r##"(?:token|access_token|auth_token)\s*[:=]\s*["\']?[\w\-]{20,}"##,
    ),
    (
        ThreatType::CredentialHarvesting,
        r##"(?:secret|client_secret|api_secret)\s*[:=]\s*["\']?[\w\-]{20,}"##,
    ),
    (
        ThreatType::CredentialHarvesting,
        r##"(?:username|user|login)\s*[:=]\s*["\']?\w{3,}"##,
    ),
    (ThreatType::CredentialHarvesting, r##"sk-[a-zA-Z0-9]{20,}"##),
    (
        ThreatType::CredentialHarvesting,
        r##"xoxb-[a-zA-Z0-9\-]{10,}"##,
    ),
    (
        ThreatType::CredentialHarvesting,
        r##"anthropic-[a-zA-Z0-9]{20,}"##,
    ),
];
