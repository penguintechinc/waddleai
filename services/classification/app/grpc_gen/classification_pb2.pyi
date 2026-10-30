from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FilterPhase(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FILTER_PHASE_UNSPECIFIED: _ClassVar[FilterPhase]
    FILTER_PHASE_INPUT: _ClassVar[FilterPhase]
    FILTER_PHASE_OUTPUT: _ClassVar[FilterPhase]

class FailMode(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FAIL_MODE_UNSPECIFIED: _ClassVar[FailMode]
    FAIL_MODE_OPEN: _ClassVar[FailMode]
    FAIL_MODE_CLOSED: _ClassVar[FailMode]

class EntityAction(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ENTITY_ACTION_UNSPECIFIED: _ClassVar[EntityAction]
    ENTITY_ACTION_LOG: _ClassVar[EntityAction]
    ENTITY_ACTION_REDACT: _ClassVar[EntityAction]
    ENTITY_ACTION_BLOCK: _ClassVar[EntityAction]

class NerStatus(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    NER_STATUS_UNSPECIFIED: _ClassVar[NerStatus]
    NER_STATUS_OK: _ClassVar[NerStatus]
    NER_STATUS_GATE_DISABLED: _ClassVar[NerStatus]
    NER_STATUS_BACKEND_UNAVAILABLE: _ClassVar[NerStatus]
    NER_STATUS_DEGRADED: _ClassVar[NerStatus]

class DegradedReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    DEGRADED_REASON_UNSPECIFIED: _ClassVar[DegradedReason]
    DEGRADED_REASON_BACKEND_UNAVAILABLE: _ClassVar[DegradedReason]
    DEGRADED_REASON_WORKER_FAILURE: _ClassVar[DegradedReason]
    DEGRADED_REASON_ANALYSIS_ERROR: _ClassVar[DegradedReason]
    DEGRADED_REASON_OVERLOADED: _ClassVar[DegradedReason]
    DEGRADED_REASON_TIMEOUT: _ClassVar[DegradedReason]

class EntityType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    ENTITY_TYPE_UNSPECIFIED: _ClassVar[EntityType]
    ENTITY_TYPE_PERSON: _ClassVar[EntityType]
    ENTITY_TYPE_US_SSN: _ClassVar[EntityType]
    ENTITY_TYPE_US_PASSPORT: _ClassVar[EntityType]
    ENTITY_TYPE_US_DRIVER_LICENSE: _ClassVar[EntityType]
    ENTITY_TYPE_MEDICAL_LICENSE: _ClassVar[EntityType]
    ENTITY_TYPE_EMAIL_ADDRESS: _ClassVar[EntityType]
    ENTITY_TYPE_PHONE_NUMBER: _ClassVar[EntityType]
    ENTITY_TYPE_CREDIT_CARD: _ClassVar[EntityType]
    ENTITY_TYPE_IBAN_CODE: _ClassVar[EntityType]
    ENTITY_TYPE_CRYPTO: _ClassVar[EntityType]
    ENTITY_TYPE_LOCATION: _ClassVar[EntityType]
    ENTITY_TYPE_IP_ADDRESS: _ClassVar[EntityType]
    ENTITY_TYPE_URL: _ClassVar[EntityType]
    ENTITY_TYPE_NRP: _ClassVar[EntityType]
    ENTITY_TYPE_DATE_TIME: _ClassVar[EntityType]
    ENTITY_TYPE_ORG: _ClassVar[EntityType]
    ENTITY_TYPE_OTHER: _ClassVar[EntityType]
FILTER_PHASE_UNSPECIFIED: FilterPhase
FILTER_PHASE_INPUT: FilterPhase
FILTER_PHASE_OUTPUT: FilterPhase
FAIL_MODE_UNSPECIFIED: FailMode
FAIL_MODE_OPEN: FailMode
FAIL_MODE_CLOSED: FailMode
ENTITY_ACTION_UNSPECIFIED: EntityAction
ENTITY_ACTION_LOG: EntityAction
ENTITY_ACTION_REDACT: EntityAction
ENTITY_ACTION_BLOCK: EntityAction
NER_STATUS_UNSPECIFIED: NerStatus
NER_STATUS_OK: NerStatus
NER_STATUS_GATE_DISABLED: NerStatus
NER_STATUS_BACKEND_UNAVAILABLE: NerStatus
NER_STATUS_DEGRADED: NerStatus
DEGRADED_REASON_UNSPECIFIED: DegradedReason
DEGRADED_REASON_BACKEND_UNAVAILABLE: DegradedReason
DEGRADED_REASON_WORKER_FAILURE: DegradedReason
DEGRADED_REASON_ANALYSIS_ERROR: DegradedReason
DEGRADED_REASON_OVERLOADED: DegradedReason
DEGRADED_REASON_TIMEOUT: DegradedReason
ENTITY_TYPE_UNSPECIFIED: EntityType
ENTITY_TYPE_PERSON: EntityType
ENTITY_TYPE_US_SSN: EntityType
ENTITY_TYPE_US_PASSPORT: EntityType
ENTITY_TYPE_US_DRIVER_LICENSE: EntityType
ENTITY_TYPE_MEDICAL_LICENSE: EntityType
ENTITY_TYPE_EMAIL_ADDRESS: EntityType
ENTITY_TYPE_PHONE_NUMBER: EntityType
ENTITY_TYPE_CREDIT_CARD: EntityType
ENTITY_TYPE_IBAN_CODE: EntityType
ENTITY_TYPE_CRYPTO: EntityType
ENTITY_TYPE_LOCATION: EntityType
ENTITY_TYPE_IP_ADDRESS: EntityType
ENTITY_TYPE_URL: EntityType
ENTITY_TYPE_NRP: EntityType
ENTITY_TYPE_DATE_TIME: EntityType
ENTITY_TYPE_ORG: EntityType
ENTITY_TYPE_OTHER: EntityType

class AnalyzeRequest(_message.Message):
    __slots__ = ("api_version", "text", "language", "phase", "org_public_id", "flag_distinct_id", "disabled_entity_labels", "fail_mode")
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    TEXT_FIELD_NUMBER: _ClassVar[int]
    LANGUAGE_FIELD_NUMBER: _ClassVar[int]
    PHASE_FIELD_NUMBER: _ClassVar[int]
    ORG_PUBLIC_ID_FIELD_NUMBER: _ClassVar[int]
    FLAG_DISTINCT_ID_FIELD_NUMBER: _ClassVar[int]
    DISABLED_ENTITY_LABELS_FIELD_NUMBER: _ClassVar[int]
    FAIL_MODE_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    text: str
    language: str
    phase: FilterPhase
    org_public_id: str
    flag_distinct_id: str
    disabled_entity_labels: _containers.RepeatedScalarFieldContainer[str]
    fail_mode: FailMode
    def __init__(self, api_version: _Optional[str] = ..., text: _Optional[str] = ..., language: _Optional[str] = ..., phase: _Optional[_Union[FilterPhase, str]] = ..., org_public_id: _Optional[str] = ..., flag_distinct_id: _Optional[str] = ..., disabled_entity_labels: _Optional[_Iterable[str]] = ..., fail_mode: _Optional[_Union[FailMode, str]] = ...) -> None: ...

class EntitySpan(_message.Message):
    __slots__ = ("entity_type", "entity_label", "start_cp", "end_cp", "start_byte", "end_byte", "raw_score", "confidence", "action")
    ENTITY_TYPE_FIELD_NUMBER: _ClassVar[int]
    ENTITY_LABEL_FIELD_NUMBER: _ClassVar[int]
    START_CP_FIELD_NUMBER: _ClassVar[int]
    END_CP_FIELD_NUMBER: _ClassVar[int]
    START_BYTE_FIELD_NUMBER: _ClassVar[int]
    END_BYTE_FIELD_NUMBER: _ClassVar[int]
    RAW_SCORE_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_FIELD_NUMBER: _ClassVar[int]
    ACTION_FIELD_NUMBER: _ClassVar[int]
    entity_type: EntityType
    entity_label: str
    start_cp: int
    end_cp: int
    start_byte: int
    end_byte: int
    raw_score: float
    confidence: float
    action: EntityAction
    def __init__(self, entity_type: _Optional[_Union[EntityType, str]] = ..., entity_label: _Optional[str] = ..., start_cp: _Optional[int] = ..., end_cp: _Optional[int] = ..., start_byte: _Optional[int] = ..., end_byte: _Optional[int] = ..., raw_score: _Optional[float] = ..., confidence: _Optional[float] = ..., action: _Optional[_Union[EntityAction, str]] = ...) -> None: ...

class AnalyzeResponse(_message.Message):
    __slots__ = ("spans", "ner_status", "backend", "degraded", "degraded_reason", "block_recommended", "cache_hit", "analyzed_chars", "truncated", "model_fingerprint")
    SPANS_FIELD_NUMBER: _ClassVar[int]
    NER_STATUS_FIELD_NUMBER: _ClassVar[int]
    BACKEND_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_FIELD_NUMBER: _ClassVar[int]
    DEGRADED_REASON_FIELD_NUMBER: _ClassVar[int]
    BLOCK_RECOMMENDED_FIELD_NUMBER: _ClassVar[int]
    CACHE_HIT_FIELD_NUMBER: _ClassVar[int]
    ANALYZED_CHARS_FIELD_NUMBER: _ClassVar[int]
    TRUNCATED_FIELD_NUMBER: _ClassVar[int]
    MODEL_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    spans: _containers.RepeatedCompositeFieldContainer[EntitySpan]
    ner_status: NerStatus
    backend: str
    degraded: bool
    degraded_reason: DegradedReason
    block_recommended: bool
    cache_hit: bool
    analyzed_chars: int
    truncated: bool
    model_fingerprint: str
    def __init__(self, spans: _Optional[_Iterable[_Union[EntitySpan, _Mapping]]] = ..., ner_status: _Optional[_Union[NerStatus, str]] = ..., backend: _Optional[str] = ..., degraded: _Optional[bool] = ..., degraded_reason: _Optional[_Union[DegradedReason, str]] = ..., block_recommended: _Optional[bool] = ..., cache_hit: _Optional[bool] = ..., analyzed_chars: _Optional[int] = ..., truncated: _Optional[bool] = ..., model_fingerprint: _Optional[str] = ...) -> None: ...

class GetEntityConfigRequest(_message.Message):
    __slots__ = ("api_version",)
    API_VERSION_FIELD_NUMBER: _ClassVar[int]
    api_version: str
    def __init__(self, api_version: _Optional[str] = ...) -> None: ...

class EntityConfigItem(_message.Message):
    __slots__ = ("entity_type", "entity_label", "default_action", "confidence_weight")
    ENTITY_TYPE_FIELD_NUMBER: _ClassVar[int]
    ENTITY_LABEL_FIELD_NUMBER: _ClassVar[int]
    DEFAULT_ACTION_FIELD_NUMBER: _ClassVar[int]
    CONFIDENCE_WEIGHT_FIELD_NUMBER: _ClassVar[int]
    entity_type: EntityType
    entity_label: str
    default_action: EntityAction
    confidence_weight: float
    def __init__(self, entity_type: _Optional[_Union[EntityType, str]] = ..., entity_label: _Optional[str] = ..., default_action: _Optional[_Union[EntityAction, str]] = ..., confidence_weight: _Optional[float] = ...) -> None: ...

class GetEntityConfigResponse(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[EntityConfigItem]
    def __init__(self, items: _Optional[_Iterable[_Union[EntityConfigItem, _Mapping]]] = ...) -> None: ...
