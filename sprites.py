"""Loads and slices the pixel-art asset pack in Sprites/.

load_sprites() must be called after pygame.display.set_mode(), since
convert_alpha() requires an active display surface.
"""

import os

import pygame

from shared import settings

SPRITE_DIR = os.path.join(os.path.dirname(__file__), "Sprites")

TILE_FRAME = 16
PLAYER_FRAME_W = 16
PLAYER_FRAME_H = 24

# Row index within a PlayerXWalk.png sheet for each facing direction.
PLAYER_DIRECTIONS = {"down": 1, "up": 0, "left": 2, "right": 3}
PLAYER_WALK_FRAMES = 4
PLAYER_DEATH_FRAMES = 6

# Sprite-sheet color name for each team index - matches settings.TEAM_COLORS order.
TEAM_SPRITE_NAMES = {0: "Red", 1: "Blue"}


def _load(name):
    return pygame.image.load(os.path.join(SPRITE_DIR, name)).convert_alpha()


def _slice_row(sheet, frame_w, frame_h, count, row=0):
    frames = []
    for i in range(count):
        rect = pygame.Rect(i * frame_w, row * frame_h, frame_w, frame_h)
        frames.append(sheet.subsurface(rect).copy())
    return frames


def _scale_all(frames, size):
    return [pygame.transform.scale(f, size) for f in frames]


class Sprites:
    """Plain data holder - see load_sprites() for what gets populated."""


def load_sprites():
    s = Sprites()
    tile_size = (settings.CELL_SIZE, settings.CELL_SIZE)
    player_size = (settings.CELL_SIZE, round(settings.CELL_SIZE * PLAYER_FRAME_H / PLAYER_FRAME_W))

    s.ground = pygame.transform.scale(_load("Ground.png"), tile_size)
    s.ground_shadow = pygame.transform.scale(_load("GroundShadow.png"), tile_size)
    s.block = pygame.transform.scale(_load("Block.png"), tile_size)
    s.brick = pygame.transform.scale(_load("Brick.png"), tile_size)
    s.brick_destroy = _scale_all(_slice_row(_load("BrickDestroy.png"), TILE_FRAME, TILE_FRAME, 7), tile_size)

    s.bomb = _scale_all(_slice_row(_load("Bomb.png"), TILE_FRAME, TILE_FRAME, 4), tile_size)

    explosion_start = _scale_all(_slice_row(_load("ExplosionStart.png"), TILE_FRAME, TILE_FRAME, 8), tile_size)
    explosion_middle = _scale_all(_slice_row(_load("ExplosionMiddle.png"), TILE_FRAME, TILE_FRAME, 8), tile_size)
    explosion_end = _scale_all(_slice_row(_load("ExplosionEnd.png"), TILE_FRAME, TILE_FRAME, 8), tile_size)

    # ExplosionMiddle is authored as a horizontal bar; ExplosionEnd points right.
    # Precompute the rotated variants once so rendering doesn't rotate per-frame.
    s.explosion_start = explosion_start
    s.explosion_middle = {
        "mid_h": explosion_middle,
        "mid_v": [pygame.transform.rotate(f, 90) for f in explosion_middle],
    }
    s.explosion_end = {
        "end_right": explosion_end,
        "end_up": [pygame.transform.rotate(f, 90) for f in explosion_end],
        "end_left": [pygame.transform.rotate(f, 180) for f in explosion_end],
        "end_down": [pygame.transform.rotate(f, -90) for f in explosion_end],
    }

    s.item_bomb = pygame.transform.scale(_load("ItemExtraBomb.png"), tile_size)
    s.item_fire = pygame.transform.scale(_load("ItemBlastRadius.png"), tile_size)
    s.item_speed = pygame.transform.scale(_load("ItemSpeedIncrease.png"), tile_size)

    s.player_walk = {}
    s.player_death = {}
    for team, name in TEAM_SPRITE_NAMES.items():
        walk_sheet = _load(f"Player{name}Walk.png")
        s.player_walk[team] = {
            direction: _scale_all(
                _slice_row(walk_sheet, PLAYER_FRAME_W, PLAYER_FRAME_H, PLAYER_WALK_FRAMES, row=row),
                player_size,
            )
            for direction, row in PLAYER_DIRECTIONS.items()
        }
        death_sheet = _load(f"Player{name}Death.png")
        s.player_death[team] = _scale_all(
            _slice_row(death_sheet, PLAYER_FRAME_W, PLAYER_FRAME_H, PLAYER_DEATH_FRAMES),
            player_size,
        )

    return s
