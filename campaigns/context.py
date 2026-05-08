"""
CampaignContext — immutable configuration object for one outbound campaign.

Loaded at startup from skills/{campaign_id}/ by CampaignLoader.
Passed explicitly through the entire pipeline so there is no global mutable state.
"""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CampaignContext:
    id: str                              # "seedance", "kling"
    name: str                            # "Seedance", "Kling AI"
    stage1_system_prompt: str            # assembled Stage 1 system prompt
    stage2_system_prompt: str            # assembled Stage 2 system prompt
    score_weights: dict = field(default_factory=dict)   # {field: max_points}
    qualified_threshold: int = 70
    disqualified_threshold: int = 60

    def __post_init__(self):
        if not self.score_weights:
            # Prevent accidental empty weights — fall back to standard 100-pt model
            object.__setattr__(self, "score_weights", {
                "segment_fit": 30,
                "buyer_fit": 25,
                "pain_fit": 20,
                "workflow_fit": 15,
                "commercial_fit": 10,
            })
