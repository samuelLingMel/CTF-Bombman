DEFAULT_PORT = 5555
TICK_RATE = 30
TICK_INTERVAL = 1 / TICK_RATE

MAX_NAME_LENGTH = 16
# How long the server waits, right after accepting a connection, for the
# client's "hello" (display name) message before giving up and falling back
# to a generic name - so a slow/misbehaving client can't stall the accept
# thread forever.
NAME_HANDSHAKE_TIMEOUT_SECONDS = 5.0

# How far behind real-time the client renders, so it can always interpolate
# between two already-confirmed positions instead of guessing ahead of the
# latest one. Guessing ahead (dead reckoning) looks smooth until timing
# jitters, then visibly snaps back to correct itself; this trades a small,
# constant, unnoticeable latency (imperceptible on LAN) to avoid that entirely.
RENDER_INTERP_DELAY_MS = round(TICK_INTERVAL * 1000)

CELL_SIZE = 50
GRID_COLS = 17
GRID_ROWS = 13
FIELD_WIDTH = GRID_COLS * CELL_SIZE
FIELD_HEIGHT = GRID_ROWS * CELL_SIZE

PLAYER_SIZE = round(CELL_SIZE * 0.9 * 0.8)  # 90% of a tile, then shrunk to 80% of that
GRID_MOVE_SPEED = 4.5  # cells per second
PLAYER_SPEED = CELL_SIZE * GRID_MOVE_SPEED  # pixels per second while stepping between cells

# Empty margin around the hitbox within a tile, and how much crossing-progress it
# takes before the hitbox's leading edge actually reaches the next cell. Smaller
# PLAYER_SIZE -> bigger gap -> narrower window where a crossing counts as touching
# both cells (see Player.occupied_cells in server.py).
PLAYER_PADDING = (CELL_SIZE - PLAYER_SIZE) / 2
HITBOX_GAP_FRACTION = PLAYER_PADDING / CELL_SIZE

# Progress fractions (0=still in source cell, 1=arrived in destination cell) a player
# can be released to hold at mid-crossing. This is a game-feel choice, independent
# of hitbox size.
MOVE_CHECKPOINTS = (0.10, 0.25, 0.50, 0.75, 0.90)

PLAYER_COLORS = [
    (220, 60, 60),
    (60, 120, 220),
    (60, 200, 100),
    (230, 200, 60),
]

# Player spawns: north/south, centered horizontally, directly opposite each
# other, right against the map edge - like the flag homes, this gives each
# only 3 open approach paths (the map boundary blocks the 4th) instead of 4.
PLAYER_SPAWNS = [
    (GRID_COLS // 2, 0),               # team 0 (Red) - north edge
    (GRID_COLS // 2, GRID_ROWS - 1),   # team 1 (Blue) - south edge
]

# Fraction of open (non-hard-wall, non-spawn-safe) cells that become soft walls.
SOFT_WALL_DENSITY = 0.65

MAX_BOMBS_PER_PLAYER = 2
BOMB_FUSE_SECONDS = 3.0
BOMB_BLAST_RANGE = 4  # cells in each direction from the bomb
BLAST_DURATION_SECONDS = 0.4  # how long the blast is drawn/hazardous for

# CTF: two teams. Players spawn north/south (above); flag bases sit west/east,
# right against the map edge so each only has 3 open approach paths (the 4th
# side is the boundary itself) instead of the usual 4.
TEAM_COUNT = 2
TEAM_NAMES = ("Red", "Blue")
TEAM_COLORS = [
    (220, 60, 60),
    (60, 120, 220),
]
UNASSIGNED_COLOR = (150, 150, 160)  # players in the lobby who haven't picked a team yet
FLAG_HOMES = [
    (0, GRID_ROWS // 2),               # team 0 (Red) - west edge
    (GRID_COLS - 1, GRID_ROWS // 2),   # team 1 (Blue) - east edge
]

FLAG_CARRY_SPEED = 3.5  # cells/sec, replaces the carrier's normal speed entirely (not a multiplier)
CAPTURES_TO_WIN = 2

# Power-ups: a destroyed soft wall has a chance to reveal one. They sit on the
# ground until picked up (or wiped out by a later blast passing over them),
# and a player's power level resets to base stats when they're hit and respawn.
POWER_UP_SPAWN_CHANCE = 0.70
POWER_UP_KINDS = ("bomb", "fire", "speed", "disease", "timer", "kick")
# Relative odds among POWER_UP_KINDS once a spawn happens: bomb/fire/speed >
# disease > timer/kick.
POWER_UP_WEIGHTS = (5, 5, 5, 2, 1, 1)
POWER_UP_COLORS = {
    "bomb": (200, 80, 80),
    "fire": (240, 140, 40),
    "speed": (80, 190, 230),
    "disease": (160, 60, 200),
    "timer": (230, 220, 90),
    "kick": (120, 200, 120),
}
POWER_UP_LABELS = {"bomb": "B", "fire": "F", "speed": "S", "disease": "?", "timer": "T", "kick": "K"}

KICK_SPEED = 8.0  # cells per second while a kicked bomb is sliding

BOMB_CAPACITY_INCREMENT = 1
MAX_BOMB_CAPACITY = 6

BLAST_RANGE_INCREMENT = 2
MAX_BLAST_RANGE = 6

SPEED_INCREMENT = 1.0  # cells per second added per Speed Up pickup
MAX_SPEED_BONUS = 4.0  # cells per second, cap

# Diseases: a random timed status effect, picked up from a "disease" power-up.
# Touching another player gives them the exact same disease (not a re-roll),
# with their own fresh timer - the source keeps their disease and timer as-is.
# Dying cures it. Afflicted players just blink black/white on screen, so the
# specific effect isn't revealed at a glance.
DISEASE_DURATION_SECONDS = 12.0
DISEASE_KINDS = (
    "superspeed", "slowdown", "immortality", "walk_through_walls", "blow_through_walls", "reversed_controls",
)
# cells/sec, replaces the afflicted player's normal speed entirely (not multipliers)
DISEASE_SUPERSPEED = 12.0
DISEASE_SLOWDOWN = 2.5

# Client-side sprite animation timings (ms per frame), purely cosmetic.
WALK_FRAME_MS = 100
BOMB_FRAME_MS = 150
EXPLOSION_FRAME_MS = round(BLAST_DURATION_SECONDS * 1000 / 8)  # 8 frames spread over the blast's lifetime
DESTROY_FRAME_MS = 50  # 7-frame brick-crumble animation
DEATH_FRAME_MS = 150  # 6-frame death animation
DEATH_JUMP_THRESHOLD = CELL_SIZE * 1.5  # bigger-than-possible position jump = treat as a respawn
