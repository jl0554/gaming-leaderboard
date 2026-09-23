from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StringConstraints

Identifier = Annotated[
    str,
    StringConstraints(
        strict=True, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"
    ),
]
Score = Annotated[StrictInt, Field(ge=0, le=1_000_000_000)]


class ScoreSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: Identifier
    score: Score


class PlayerEntry(BaseModel):
    user_id: str
    score: int
    rank: int = Field(ge=1)


class ScoreResponse(PlayerEntry):
    game_id: str
    updated: bool


class LeaderboardResponse(BaseModel):
    game_id: str
    entries: list[PlayerEntry]


class ContextResponse(BaseModel):
    game_id: str
    player: PlayerEntry
    above: list[PlayerEntry]
    below: list[PlayerEntry]
