from redis.asyncio import Redis

from leaderboard.models import ContextResponse, PlayerEntry, ScoreResponse

# Store -score so Redis's ascending order is public score DESC, player ID ASC.
# LT accepts better public scores atomically, while still inserting new players.
SUBMIT_SCRIPT = """
local changed = redis.call('ZADD', KEYS[1], 'LT', 'CH', ARGV[2], ARGV[1])
local stored_score = redis.call('ZSCORE', KEYS[1], ARGV[1])
local rank = 1 + redis.call('ZCOUNT', KEYS[1], '-inf', '(' .. stored_score)
return {stored_score, rank, changed}
"""

# Position locates neighbors; counts over the ENTIRE game determine shared rank.
# The HTTP radius cap bounds this script to at most 21 players.
CONTEXT_SCRIPT = """
local position = redis.call('ZRANK', KEYS[1], ARGV[1])
if not position then
    return {}
end
local radius = tonumber(ARGV[2])
local start = math.max(0, position - radius)
local rows = redis.call('ZRANGE', KEYS[1], start, position + radius, 'WITHSCORES')
local entries = {}
local ranks = {}
for i = 1, #rows, 2 do
    local stored_score = rows[i + 1]
    if not ranks[stored_score] then
        ranks[stored_score] = 1 + redis.call('ZCOUNT', KEYS[1], '-inf', '(' .. stored_score)
    end
    table.insert(entries, {rows[i], stored_score, ranks[stored_score]})
end
return {position - start, entries}
"""


class PlayerNotRankedError(Exception):
    """The requested player has no entry in this game's leaderboard."""


class LeaderboardStore:
    def __init__(self, redis: Redis, key_prefix: str) -> None:
        self.redis = redis
        self.key_prefix = key_prefix
        self._submit = redis.register_script(SUBMIT_SCRIPT)
        self._context = redis.register_script(CONTEXT_SCRIPT)

    def _key(self, game_id: str) -> str:
        return f"{self.key_prefix}:game:{game_id}"

    async def submit(self, game_id: str, user_id: str, score: int) -> ScoreResponse:
        stored_score, rank, changed = await self._submit(
            keys=[self._key(game_id)], args=[user_id, -score]
        )
        return ScoreResponse(
            game_id=game_id,
            user_id=user_id,
            score=-int(float(stored_score)),
            rank=int(rank),
            updated=bool(changed),
        )

    async def top(self, game_id: str, limit: int) -> list[PlayerEntry]:
        rows = await self.redis.zrange(self._key(game_id), 0, limit - 1, withscores=True)
        entries = []
        previous_score = None
        rank = 0
        # This is a complete prefix from ONE snapshot. New score groups take
        # their first display position; equal scores retain the group's rank.
        for position, (user_id, stored_score) in enumerate(rows, start=1):
            score = -int(stored_score)
            if score != previous_score:
                rank = position
            entries.append(PlayerEntry(user_id=user_id, score=score, rank=rank))
            previous_score = score
        return entries

    async def context(self, game_id: str, user_id: str, radius: int) -> ContextResponse:
        result = await self._context(keys=[self._key(game_id)], args=[user_id, radius])
        if not result:
            raise PlayerNotRankedError
        target_index, rows = result
        entries = [
            PlayerEntry(user_id=row[0], score=-int(float(row[1])), rank=int(row[2]))
            for row in rows
        ]
        target_index = int(target_index)
        return ContextResponse(
            game_id=game_id,
            player=entries[target_index],
            above=entries[:target_index],
            below=entries[target_index + 1 :],
        )
