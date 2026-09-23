from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request

from leaderboard.models import (
    ContextResponse,
    Identifier,
    LeaderboardResponse,
    ScoreResponse,
    ScoreSubmission,
)
from leaderboard.security import require_submission_key

router = APIRouter(prefix="/games", tags=["leaderboard"])


@router.post(
    "/{game_id}/scores",
    response_model=ScoreResponse,
    dependencies=[Depends(require_submission_key)],
    responses={401: {"description": "Invalid or missing submission API key"}},
)
async def submit_score(
    game_id: Identifier, submission: ScoreSubmission, request: Request
) -> ScoreResponse:
    return await request.app.state.leaderboard.submit(
        game_id, submission.user_id, submission.score
    )


@router.get("/{game_id}/leaderboard", response_model=LeaderboardResponse)
async def top_players(
    game_id: Identifier,
    request: Request,
    limit: Annotated[int, Query(ge=1, le=100)] = 10,
) -> LeaderboardResponse:
    entries = await request.app.state.leaderboard.top(game_id, limit)
    return LeaderboardResponse(game_id=game_id, entries=entries)


@router.get("/{game_id}/players/{user_id}/context", response_model=ContextResponse)
async def player_context(
    game_id: Identifier,
    user_id: Identifier,
    request: Request,
    radius: Annotated[int, Query(ge=0, le=10)] = 1,
) -> ContextResponse:
    return await request.app.state.leaderboard.context(game_id, user_id, radius)
