import logging
from typing import Text, Dict, Union, List, Optional

from rasax.community import config, telemetry
from rasax.community.database import PlatformFeature
from rasax.community.database.service import DbService

logger = logging.getLogger(__name__)


class FeatureService(DbService):
    def set_feature(self, feature: Dict[Text, Union[Text, bool]]) -> None:
        platform_feature = (
            self.query(PlatformFeature)
            .filter(PlatformFeature.feature_name == feature["name"])
            .first()
        )

        if platform_feature:
            platform_feature.enabled = feature["enabled"]
        else:
            platform_feature = PlatformFeature(
                feature_name=feature["name"], enabled=feature["enabled"]
            )
            self.add(platform_feature)

        telemetry.track_feature_flag(
            platform_feature.feature_name, platform_feature.enabled
        )

    @staticmethod
    def _is_contained_in_list(
        feature_name: Text, feature_list: List[Dict[Text, Union[Text, bool]]]
    ) -> Optional[Dict[Text, Union[Text, bool]]]:
        for r in feature_list:
            if r.get("name") == feature_name:
                return r
        return None

    def _merge_with_defaults(
        self, existing_feature_list: List[Dict[Text, Union[Text, bool]]]
    ) -> List[Dict[Text, Union[Text, bool]]]:
        new_feature_list = []
        for flag_default in self._default_feature_flags():
            existing_feature = self._is_contained_in_list(
                flag_default.get("name"), existing_feature_list
            )

            new_feature_list.append(existing_feature or flag_default)
        return new_feature_list

    @staticmethod
    def _default_feature_flags() -> List[Dict[Text, Union[Text, bool]]]:

        if config.LOCAL_MODE:
            feature_flags = config.DEFAULT_LOCAL_FEATURE_FLAGS
        else:
            feature_flags = config.DEFAULT_SERVER_FEATURE_FLAGS

        return feature_flags

    def features(self) -> List[Dict[Text, Union[Text, bool]]]:
        platform_features = self.query(PlatformFeature).all()
        platform_features = [p.as_dict() for p in platform_features]

        return self._merge_with_defaults(platform_features)
