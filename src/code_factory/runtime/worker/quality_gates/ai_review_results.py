from __future__ import annotations

from dataclasses import dataclass

from ....coding_agents.review_models import ReviewFinding, ReviewOutput
from ....workflow.profiles.review_profiles import WorkflowReviewType
from ....workspace.review.review_surface import WorktreeReviewSelection


@dataclass(frozen=True, slots=True)
class ExecutedAiReview:
    review_type: WorkflowReviewType
    review_output: ReviewOutput
    accepted_findings: tuple[ReviewFinding, ...]


@dataclass(frozen=True, slots=True)
class AiReviewPassResult:
    selection: WorktreeReviewSelection
    executed_reviews: tuple[ExecutedAiReview, ...]
    capped_review_types: tuple[WorkflowReviewType, ...] = ()

    @property
    def accepted_findings(self) -> tuple[ReviewFinding, ...]:
        return tuple(
            finding
            for review in self.executed_reviews
            for finding in review.accepted_findings
        )

    @property
    def matched_review_types(self) -> tuple[WorkflowReviewType, ...]:
        return tuple(review.review_type for review in self.executed_reviews)
