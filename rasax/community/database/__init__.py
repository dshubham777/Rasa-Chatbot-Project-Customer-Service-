# this file imports all SQL tables at module level - this avoids
# reference to non-existing tables in SQL relations

# Skip pytype for this to avoid errors with circular imports
# pytype: skip-file

# noinspection PyUnresolvedReferences
from rasax.community.database.admin import (
    Project,
    User,
    PlatformFeature,
    Role,
    Environment,
    Permission,
    ChatToken,
    LocalPassword,
    SingleUseToken,
    ConfigValue,
    GitRepository,
)

# noinspection PyUnresolvedReferences
from rasax.community.database.conversation import (
    Conversation,
    ConversationActionMetadata,
    ConversationEntityMetadata,
    ConversationEvent,
    ConversationIntentMetadata,
    ConversationMessageCorrection,
    ConversationPolicyMetadata,
    MessageLog,
)

# noinspection PyUnresolvedReferences
from rasax.community.database.analytics import (
    ConversationActionStatistic,
    ConversationEntityStatistic,
    ConversationIntentStatistic,
    ConversationPolicyStatistic,
    ConversationStatistic,
    ConversationSession,
    AnalyticsCache,
)

# noinspection PyUnresolvedReferences
from rasax.community.database.data import (
    TrainingData,
    Response,
    Story,
    EntitySynonymValue,
    EntitySynonym,
)

# noinspection PyUnresolvedReferences
from rasax.community.database.domain import (
    Domain,
    DomainAction,
    DomainEntity,
    DomainIntent,
    DomainSlot,
)

# noinspection PyUnresolvedReferences
from rasax.community.database.intent import (
    Intent,
    UserGoal,
    TemporaryIntentExample,
    UserGoalIntent,
)

# noinspection PyUnresolvedReferences
from rasax.community.database.model import (
    Model,
    ModelTag,
    NluEvaluation,
    NluEvaluationPrediction,
)
