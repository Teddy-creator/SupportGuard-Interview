from __future__ import annotations

from supportguard.services.segment_approval import ApprovalResumeSegments
from supportguard.services.segment_base import SegmentTransactionBase
from supportguard.services.segment_dispatch import RunDispatchSegments
from supportguard.services.segment_finalization import FinalizationSegments
from supportguard.services.segment_recovery import RecoverySegments


class SegmentRepository(
    FinalizationSegments,
    ApprovalResumeSegments,
    RunDispatchSegments,
    RecoverySegments,
    SegmentTransactionBase,
):
    """Stable composition root for fenced Segment transaction owners."""

    pass
