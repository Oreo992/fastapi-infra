from enum import Enum


class FeatureFlag(str, Enum):
    ENABLED = "enabled"
    DISABLED = "disabled"
    AUTO = "auto"


def resolve_feature_flag(value: bool | None) -> FeatureFlag:
    if value is True:
        return FeatureFlag.ENABLED
    if value is False:
        return FeatureFlag.DISABLED
    return FeatureFlag.AUTO
