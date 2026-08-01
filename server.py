"""Authoritative game server.

Owns all player state. Clients only send inputs and receive state broadcasts
- they never decide their own position. Run standalone for a dedicated
server, or imported and started in a background thread by a hosting client.
"""

import random
import socket
import threading
import time

from shared import settings
from shared.protocol import send_message, MessageReader

DIRECTIONS = {(1, 0): "right", (-1, 0): "left", (0, 1): "down", (0, -1): "up"}


def generate_walls():
    """Hard (indestructible) walls in a pillar pattern, plus soft (destructible)
    walls randomly filling the rest - except a cleared safe zone around each
    player spawn and flag home so nobody starts boxed in.

    Player spawns (north/south) and flag homes (west/east) are each other's
    180-degree-rotation mirror, so every cell is decided together with its
    mirror (same call for both) rather than independently - otherwise one
    team's approach could end up measurably more walled off than the
    other's purely by chance, which isn't fair for a symmetric map.
    """
    width, height = settings.GRID_COLS, settings.GRID_ROWS

    safe_cells = set()
    for sc, sr in settings.PLAYER_SPAWNS + settings.FLAG_HOMES:
        safe_cells.add((sc, sr))
        for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c, r = sc + dc, sr + dr
            if 0 <= c < width and 0 <= r < height:
                safe_cells.add((c, r))

    hard_walls = set()
    soft_walls = set()
    assigned = set()

    for c in range(width):
        for r in range(height):
            cell = (c, r)
            if cell in assigned:
                continue
            mirror = (width - 1 - c, height - 1 - r)
            assigned.add(cell)
            assigned.add(mirror)

            is_pillar = c % 2 == 1 and r % 2 == 1
            place_soft = not is_pillar and random.random() < settings.SOFT_WALL_DENSITY

            for target in {cell, mirror}:
                if target in safe_cells:
                    continue
                if is_pillar:
                    hard_walls.add(target)
                elif place_soft:
                    soft_walls.add(target)

    return hard_walls, soft_walls


class Bomb:
    def __init__(self, col, row, owner_id, planted_at, blast_range, pierce=False):
        self.col = col
        self.row = row
        self.x = float(col * settings.CELL_SIZE)
        self.y = float(row * settings.CELL_SIZE)
        self.owner_id = owner_id
        self.planted_at = planted_at
        self.blast_range = blast_range  # snapshot of the owner's power at placement time
        self.pierce = pierce  # "blow through walls" disease - blast ignores walls

        # kicked-bomb sliding state - move_dx/move_dy is the slide direction
        # (0,0) if resting; target_col/row is the in-progress single-cell hop
        self.move_dx = 0
        self.move_dy = 0
        self.target_col = None
        self.target_row = None

        self.force_detonate = False  # set by the owner's remote detonator


class PowerUp:
    def __init__(self, col, row, kind):
        self.col = col
        self.row = row
        self.kind = kind  # "bomb" | "fire" | "speed"


class Flag:
    """A team's flag. Lives at home until an enemy steals it; while carried it
    follows the carrier; if the carrier is hit it drops where they were. Only
    a teammate of the flag's owning team can return a dropped flag home.
    """

    def __init__(self, team, home_col, home_row):
        self.team = team
        self.home_col = home_col
        self.home_row = home_row
        self.carrier_id = None
        self.state = "home"  # "home" | "carried" | "dropped"
        self.col = home_col
        self.row = home_row
        self.x = float(home_col * settings.CELL_SIZE)
        self.y = float(home_row * settings.CELL_SIZE)

    def is_home(self):
        return self.state == "home"

    def return_home(self):
        self.state = "home"
        self.carrier_id = None
        self.col, self.row = self.home_col, self.home_row
        self.x = float(self.home_col * settings.CELL_SIZE)
        self.y = float(self.home_row * settings.CELL_SIZE)

    def pick_up(self, player):
        self.state = "carried"
        self.carrier_id = player.id

    def drop(self, col, row):
        self.state = "dropped"
        self.carrier_id = None
        self.col, self.row = col, row
        self.x = float(col * settings.CELL_SIZE)
        self.y = float(row * settings.CELL_SIZE)

    def follow_carrier(self, player):
        self.col, self.row = player.col, player.row
        self.x, self.y = player.x, player.y


class Player:
    def __init__(self, player_id, color, spawn_col, spawn_row, team):
        self.id = player_id
        self.color = color
        self.team = team
        self.spawn_col = spawn_col
        self.spawn_row = spawn_row
        self.col = spawn_col  # last cell the player fully settled into
        self.row = spawn_row
        self.x = float(self.col * settings.CELL_SIZE)
        self.y = float(self.row * settings.CELL_SIZE)
        self.dx = 0  # requested input direction, from the client
        self.dy = 0
        self.target_col = None  # destination cell of the in-progress single-cell step
        self.target_row = None
        self.speed_override = None  # cells/sec that replaces normal speed entirely, or None

        # power-up-driven stats - reset to these base values on respawn (dying
        # costs you your power level, same as classic Bomberman)
        self.bomb_capacity = settings.MAX_BOMBS_PER_PLAYER
        self.blast_range = settings.BOMB_BLAST_RANGE
        self.speed_bonus = 0.0  # extra cells/sec from Speed Up pickups
        self.has_kick = False  # walking into a bomb pushes it, instead of blocking you
        self.has_remote = False  # can detonate all of their own bombs on command

        self.disease = None  # one of settings.DISEASE_KINDS, or None
        self.disease_expires_at = 0.0

        self.facing = "down"  # last direction faced, for sprite selection
        self.is_moving = False  # True on any tick where position actually changed

    def respawn(self):
        self.col, self.row = self.spawn_col, self.spawn_row
        self.x = float(self.col * settings.CELL_SIZE)
        self.y = float(self.row * settings.CELL_SIZE)
        self.target_col = self.target_row = None
        self.bomb_capacity = settings.MAX_BOMBS_PER_PLAYER
        self.blast_range = settings.BOMB_BLAST_RANGE
        self.speed_bonus = 0.0
        self.has_kick = False
        self.has_remote = False
        self.disease = None
        self.disease_expires_at = 0.0

    def intended_target(self):
        """The cell this player is about to start moving into this tick, or
        None if they're idle with no input or already mid-crossing. Used to
        detect "walking into a bomb" for the kick power-up, before blocked
        cells are resolved and step() actually runs.
        """
        if self.target_col is not None:
            return None
        dx, dy = self.dx, self.dy
        if self.disease == "reversed_controls":
            dx, dy = -dx, -dy
        if dx != 0:
            dy = 0
        if dx == 0 and dy == 0:
            return None
        return (self.col + dx, self.row + dy)

    def step(self, dt, blocked_cells):
        dx, dy = self.dx, self.dy
        if self.disease == "reversed_controls":
            dx, dy = -dx, -dy

        self.is_moving = False
        if dx == 1:
            self.facing = "right"
        elif dx == -1:
            self.facing = "left"
        elif dy == 1:
            self.facing = "down"
        elif dy == -1:
            self.facing = "up"

        if self.target_col is None:
            if dx != 0:
                dy = 0  # no diagonal movement - horizontal wins when both are held

            if dx == 0 and dy == 0:
                return

            col, row = self.col + dx, self.row + dy
            in_bounds = 0 <= col < settings.GRID_COLS and 0 <= row < settings.GRID_ROWS
            if not in_bounds or (col, row) in blocked_cells:
                return
            self.target_col, self.target_row = col, row
            # fall through and move this same tick - arming the target without
            # moving wasted a full tick at every cell boundary, which is what
            # produced the visible little stop-and-go stutter while holding a
            # direction continuously

        move_dx = self.target_col - self.col
        move_dy = self.target_row - self.row

        if dx == -move_dx and dy == -move_dy:
            # holding the opposite direction reverses the crossing rather than
            # forcing the player to finish arriving first
            self.col, self.target_col = self.target_col, self.col
            self.row, self.target_row = self.target_row, self.row
            move_dx, move_dy = -move_dx, -move_dy

        if dx != move_dx or dy != move_dy:
            # input no longer matches the direction of travel (released, or a
            # perpendicular tap). A perpendicular direction doesn't turn
            # instantly - the player keeps sliding toward whichever outer
            # checkpoint (10%/90%) is closer, same speed as normal movement,
            # and only pivots into the new direction once they arrive there.
            # A plain release just holds at the nearest checkpoint instead.
            if dx or dy:
                progress = self._progress()
                approach_forward = progress >= 0.5  # True -> finish toward 90%, False -> ease back to 10%
                checkpoint = settings.MOVE_CHECKPOINTS[-1] if approach_forward else settings.MOVE_CHECKPOINTS[0]
                checkpoint_x, checkpoint_y = self._position_at_progress(checkpoint)

                step_amount = self._cells_per_sec() * settings.CELL_SIZE * dt
                remaining_x = checkpoint_x - self.x
                remaining_y = checkpoint_y - self.y

                if abs(remaining_x) + abs(remaining_y) <= step_amount:
                    # reached the checkpoint - commit to the resolved cell and pivot
                    if approach_forward:
                        self.x, self.y = self.target_col * settings.CELL_SIZE, self.target_row * settings.CELL_SIZE
                        self.col, self.row = self.target_col, self.target_row
                    else:
                        self.x, self.y = self.col * settings.CELL_SIZE, self.row * settings.CELL_SIZE
                    self.target_col = self.target_row = None
                    self.step(dt, blocked_cells)  # re-enter idle so the new direction starts this tick
                    return

                self.is_moving = True
                if remaining_x != 0:
                    self.x += step_amount if remaining_x > 0 else -step_amount
                else:
                    self.y += step_amount if remaining_y > 0 else -step_amount
                return

            self._snap_to_nearest_checkpoint()
            return

        target_x = self.target_col * settings.CELL_SIZE
        target_y = self.target_row * settings.CELL_SIZE
        step_amount = self._cells_per_sec() * settings.CELL_SIZE * dt
        remaining_x = target_x - self.x
        remaining_y = target_y - self.y

        self.is_moving = True
        if abs(remaining_x) + abs(remaining_y) <= step_amount:
            self.x, self.y = target_x, target_y
            self.col, self.row = self.target_col, self.target_row
            self.target_col = self.target_row = None
        elif remaining_x != 0:
            self.x += step_amount if remaining_x > 0 else -step_amount
        else:
            self.y += step_amount if remaining_y > 0 else -step_amount

    def _cells_per_sec(self):
        """Effective movement speed for this tick. speed_override (flag-carry
        or a speed-changing disease) replaces the normal speed entirely rather
        than scaling it, since those are meant to feel like a flat, absolute
        pace rather than a percentage change.
        """
        if self.speed_override is not None:
            return self.speed_override
        return settings.GRID_MOVE_SPEED + self.speed_bonus

    def _progress(self):
        """0.0 = still fully in the source cell, 1.0 = arrived in the target cell."""
        origin_x = self.col * settings.CELL_SIZE
        origin_y = self.row * settings.CELL_SIZE
        traveled = abs(self.x - origin_x) + abs(self.y - origin_y)
        return traveled / settings.CELL_SIZE

    def _position_at_progress(self, progress):
        origin_x = self.col * settings.CELL_SIZE
        origin_y = self.row * settings.CELL_SIZE
        target_x = self.target_col * settings.CELL_SIZE
        target_y = self.target_row * settings.CELL_SIZE
        return origin_x + (target_x - origin_x) * progress, origin_y + (target_y - origin_y) * progress

    def _snap_to_nearest_checkpoint(self):
        progress = min(settings.MOVE_CHECKPOINTS, key=lambda c: abs(c - self._progress()))
        self.x, self.y = self._position_at_progress(progress)

    def occupied_cells(self):
        """Cell(s) this player's hitbox counts as touching, for collision
        (walls, bombs, other players) once those systems exist. Derived from
        actual hitbox size vs tile size: the player stays exclusively in the
        source cell until their leading edge reaches the next cell, and is
        exclusively in the destination cell once their trailing edge has
        cleared the source cell - shrinking PLAYER_SIZE narrows that window.
        """
        if self.target_col is None:
            return [(self.col, self.row)]

        progress = self._progress()
        gap = settings.HITBOX_GAP_FRACTION
        if progress <= gap:
            return [(self.col, self.row)]
        if progress >= 1 - gap:
            return [(self.target_col, self.target_row)]
        return [(self.col, self.row), (self.target_col, self.target_row)]


class GameServer:
    def __init__(self, port=settings.DEFAULT_PORT):
        self.port = port
        self.players = {}
        self.clients = {}  # player_id -> socket
        self.lock = threading.Lock()
        self.next_id = 1
        self.running = False
        self.ready = threading.Event()  # set once the socket is bound and listening

        self.hard_walls, self.soft_walls = generate_walls()
        self.blocked_cells = self.hard_walls | self.soft_walls

        self.bombs = {}  # (col, row) -> Bomb
        self.active_blasts = []  # [{"cells": {...}, "expires_at": float}, ...] - for rendering only
        self.power_ups = {}  # (col, row) -> PowerUp

        self.flags = {
            team: Flag(team, *settings.FLAG_HOMES[team])
            for team in range(settings.TEAM_COUNT)
        }
        self.scores = {team: 0 for team in range(settings.TEAM_COUNT)}
        self.winner = None

        self.match_started = False  # gameplay is frozen in a lobby until the host starts the match

    def start(self):
        self.running = True
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("0.0.0.0", self.port))
        server_sock.listen()
        print(f"[SERVER] Listening on port {self.port}")
        self.ready.set()

        threading.Thread(target=self._accept_loop, args=(server_sock,), daemon=True).start()
        self._tick_loop()

    def _accept_loop(self, server_sock):
        while self.running:
            try:
                client_sock, addr = server_sock.accept()
            except OSError:
                break
            threading.Thread(target=self._handle_client, args=(client_sock, addr), daemon=True).start()

    def _handle_client(self, client_sock, addr):
        with self.lock:
            player_id = self.next_id
            self.next_id += 1
            team = (player_id - 1) % settings.TEAM_COUNT
            color = settings.TEAM_COLORS[team]
            spawn_col, spawn_row = settings.PLAYER_SPAWNS[team]
            self.players[player_id] = Player(player_id, color, spawn_col, spawn_row, team)
            self.clients[player_id] = client_sock

        print(f"[SERVER] Player {player_id} connected from {addr} on team {settings.TEAM_NAMES[team]}")
        send_message(client_sock, {
            "type": "welcome",
            "player_id": player_id,
            "team": team,
            "match_started": self.match_started,
            "walls": {
                "hard": sorted(self.hard_walls),
                "soft": sorted(self.soft_walls),
            },
        })

        reader = MessageReader(client_sock)
        try:
            while self.running:
                messages = reader.read_messages()
                if messages is None:
                    break
                for msg in messages:
                    self._handle_message(player_id, msg)
        except (ConnectionResetError, OSError):
            pass
        finally:
            with self.lock:
                self.players.pop(player_id, None)
                self.clients.pop(player_id, None)
            client_sock.close()
            print(f"[SERVER] Player {player_id} disconnected")

    def _handle_message(self, player_id, msg):
        msg_type = msg.get("type")
        if msg_type == "input":
            with self.lock:
                player = self.players.get(player_id)
                if player:
                    player.dx = max(-1, min(1, msg.get("dx", 0)))
                    player.dy = max(-1, min(1, msg.get("dy", 0)))
        elif msg_type == "place_bomb":
            with self.lock:
                self._place_bomb(player_id)
        elif msg_type == "detonate":
            with self.lock:
                self._detonate(player_id)
        elif msg_type == "start_match":
            with self.lock:
                if player_id == 1:  # the host is whoever connected first
                    self.match_started = True

    def _detonate(self, player_id):
        if not self.match_started:
            return
        player = self.players.get(player_id)
        if player is None or not player.has_remote:
            return
        for bomb in self.bombs.values():
            if bomb.owner_id == player_id:
                bomb.force_detonate = True

    def _place_bomb(self, player_id):
        if not self.match_started:
            return
        player = self.players.get(player_id)
        if player is None:
            return

        # mid-crossing is fine, as long as they're more than halfway into a
        # cell - the bomb goes wherever they're mostly standing
        if player.target_col is None:
            cell = (player.col, player.row)
        elif player._progress() > 0.5:
            cell = (player.target_col, player.target_row)
        else:
            cell = (player.col, player.row)

        if cell in self.bombs or cell in self.blocked_cells:
            return

        bombs_owned = sum(1 for b in self.bombs.values() if b.owner_id == player_id)
        if bombs_owned >= player.bomb_capacity:
            return

        self.bombs[cell] = Bomb(
            cell[0], cell[1], player_id, time.perf_counter(), player.blast_range,
            pierce=(player.disease == "blow_through_walls"),
        )
        self.blocked_cells.add(cell)

    def _bomb_slide_blocked(self, col, row):
        if not (0 <= col < settings.GRID_COLS and 0 <= row < settings.GRID_ROWS):
            return True
        if (col, row) in self.hard_walls or (col, row) in self.soft_walls or (col, row) in self.bombs:
            return True
        return any((col, row) in player.occupied_cells() for player in self.players.values())

    def _update_kicked_bombs(self, dt):
        for bomb in list(self.bombs.values()):
            if bomb.move_dx == 0 and bomb.move_dy == 0:
                continue

            if bomb.target_col is None:
                next_col, next_row = bomb.col + bomb.move_dx, bomb.row + bomb.move_dy
                if self._bomb_slide_blocked(next_col, next_row):
                    bomb.move_dx = bomb.move_dy = 0
                    continue
                bomb.target_col, bomb.target_row = next_col, next_row

            target_x = bomb.target_col * settings.CELL_SIZE
            target_y = bomb.target_row * settings.CELL_SIZE
            step_amount = settings.KICK_SPEED * settings.CELL_SIZE * dt
            remaining_x = target_x - bomb.x
            remaining_y = target_y - bomb.y

            if abs(remaining_x) + abs(remaining_y) <= step_amount:
                bomb.x, bomb.y = target_x, target_y
                old_cell = (bomb.col, bomb.row)
                bomb.col, bomb.row = bomb.target_col, bomb.target_row
                bomb.target_col = bomb.target_row = None
                new_cell = (bomb.col, bomb.row)

                del self.bombs[old_cell]
                self.bombs[new_cell] = bomb
                self.blocked_cells.discard(old_cell)
                self.blocked_cells.add(new_cell)
                self.power_ups.pop(new_cell, None)  # a rolling bomb flattens power-ups it goes over
            elif remaining_x != 0:
                bomb.x += step_amount if remaining_x > 0 else -step_amount
            else:
                bomb.y += step_amount if remaining_y > 0 else -step_amount

    def _update_bombs(self, now):
        """Detonate any bombs whose fuse has run out (chaining into bombs
        caught in the blast), destroy soft walls hit by the blast, and hit
        any player standing in it. Returns the list of newly destroyed wall
        cells, for the caller to broadcast.
        """
        self.active_blasts = [b for b in self.active_blasts if b["expires_at"] > now]

        queue = []
        for cell, bomb in self.bombs.items():
            if bomb.force_detonate:
                queue.append(cell)
                continue
            owner = self.players.get(bomb.owner_id)
            remote_controlled = owner is not None and owner.has_remote
            if not remote_controlled and now - bomb.planted_at >= settings.BOMB_FUSE_SECONDS:
                queue.append(cell)
        if not queue:
            return []

        processed = set()
        blast_cells = set()
        blast_pieces = {}  # cell -> "start" | "mid_h" | "mid_v" | "end_<direction>", for sprite selection
        destroyed_walls = []

        while queue:
            cell = queue.pop()
            if cell in processed or cell not in self.bombs:
                continue
            processed.add(cell)

            bomb = self.bombs.pop(cell)
            self.blocked_cells.discard(cell)
            blast_cells.add(cell)
            blast_pieces[cell] = "start"
            self._blast_hits_power_up(cell)

            for (dc, dr), direction in DIRECTIONS.items():
                reached = []
                for step in range(1, bomb.blast_range + 1):
                    c, r = bomb.col + dc * step, bomb.row + dr * step
                    if not (0 <= c < settings.GRID_COLS and 0 <= r < settings.GRID_ROWS):
                        break
                    if (c, r) in self.hard_walls and not bomb.pierce:
                        break

                    reached.append((c, r))
                    blast_cells.add((c, r))
                    self._blast_hits_power_up((c, r))

                    if (c, r) in self.soft_walls:
                        self.soft_walls.discard((c, r))
                        self.blocked_cells.discard((c, r))
                        destroyed_walls.append((c, r))
                        if random.random() < settings.POWER_UP_SPAWN_CHANCE:
                            kind = random.choices(settings.POWER_UP_KINDS, weights=settings.POWER_UP_WEIGHTS)[0]
                            self.power_ups[(c, r)] = PowerUp(c, r, kind)
                        if not bomb.pierce:
                            break  # soft walls absorb the blast, unless piercing

                    if (c, r) in self.bombs:
                        queue.append((c, r))  # chain reaction

                orientation = "mid_h" if dc != 0 else "mid_v"
                for i, piece_cell in enumerate(reached):
                    blast_pieces[piece_cell] = f"end_{direction}" if i == len(reached) - 1 else orientation

        self.active_blasts.append({
            "cells": blast_cells, "pieces": blast_pieces, "expires_at": now + settings.BLAST_DURATION_SECONDS,
        })
        self._apply_blast_damage(blast_cells)
        return destroyed_walls

    def _blast_hits_power_up(self, cell):
        """A blast passing over a power-up destroys it - except a disease
        power-up, which dodges by relocating to a new random open cell.
        """
        power_up = self.power_ups.pop(cell, None)
        if power_up is None:
            return
        if power_up.kind == "disease":
            new_cell = self._random_open_cell()
            if new_cell is not None:
                power_up.col, power_up.row = new_cell
                self.power_ups[new_cell] = power_up

    def _random_open_cell(self):
        candidates = [
            (c, r)
            for c in range(settings.GRID_COLS)
            for r in range(settings.GRID_ROWS)
            if (c, r) not in self.blocked_cells and (c, r) not in self.power_ups
        ]
        return random.choice(candidates) if candidates else None

    def _apply_blast_damage(self, blast_cells):
        for player in self.players.values():
            if player.disease == "immortality":
                continue
            if any(cell in blast_cells for cell in player.occupied_cells()):
                for flag in self.flags.values():
                    if flag.carrier_id == player.id:
                        flag.drop(player.col, player.row)
                player.respawn()

    def _update_power_ups(self, now):
        if not self.power_ups:
            return

        collected = []
        for player in self.players.values():
            for cell in player.occupied_cells():
                power_up = self.power_ups.get(cell)
                if power_up is not None:
                    self._apply_power_up(player, power_up, now)
                    collected.append(cell)

        for cell in collected:
            self.power_ups.pop(cell, None)

    def _apply_power_up(self, player, power_up, now):
        if power_up.kind == "bomb":
            player.bomb_capacity = min(settings.MAX_BOMB_CAPACITY, player.bomb_capacity + settings.BOMB_CAPACITY_INCREMENT)
        elif power_up.kind == "fire":
            player.blast_range = min(settings.MAX_BLAST_RANGE, player.blast_range + settings.BLAST_RANGE_INCREMENT)
        elif power_up.kind == "speed":
            player.speed_bonus = min(settings.MAX_SPEED_BONUS, player.speed_bonus + settings.SPEED_INCREMENT)
        elif power_up.kind == "disease":
            self._afflict(player, now)
        elif power_up.kind == "timer":
            player.has_remote = True
        elif power_up.kind == "kick":
            player.has_kick = True

    def _afflict(self, player, now):
        player.disease = random.choice(settings.DISEASE_KINDS)
        player.disease_expires_at = now + settings.DISEASE_DURATION_SECONDS

    def _cure(self, player):
        player.disease = None
        player.disease_expires_at = 0.0

    def _update_diseases(self, now):
        for player in self.players.values():
            if player.disease and now >= player.disease_expires_at:
                self._cure(player)

        # contagion: touching a diseased player catches their exact disease,
        # with your own fresh timer - the source keeps theirs unchanged, so
        # both end up with the same disease but independently-expiring timers
        diseased = [p for p in self.players.values() if p.disease]
        for source in diseased:
            source_cells = set(source.occupied_cells())
            for other in self.players.values():
                if other.id == source.id or other.disease == source.disease:
                    continue
                if source_cells & set(other.occupied_cells()):
                    other.disease = source.disease
                    other.disease_expires_at = now + settings.DISEASE_DURATION_SECONDS

    def _update_flags(self):
        for team, flag in self.flags.items():
            if flag.state == "carried":
                carrier = self.players.get(flag.carrier_id)
                if carrier is None:  # carrier disconnected mid-carry
                    flag.drop(flag.col, flag.row)
                    continue

                flag.follow_carrier(carrier)

                own_flag = self.flags[carrier.team]
                at_own_base = (own_flag.home_col, own_flag.home_row) in carrier.occupied_cells()
                if at_own_base and own_flag.is_home():
                    self.scores[carrier.team] += 1
                    flag.return_home()
                    if self.scores[carrier.team] >= settings.CAPTURES_TO_WIN:
                        self.winner = carrier.team
                continue

            # not carried: check for an enemy stealing it, or a teammate
            # returning it if it's lying dropped somewhere
            for player in self.players.values():
                if (flag.col, flag.row) not in player.occupied_cells():
                    continue
                if player.team == team:
                    if flag.state == "dropped":
                        flag.return_home()
                else:
                    flag.pick_up(player)
                break

    def _tick_loop(self):
        last_time = time.perf_counter()
        while self.running:
            now = time.perf_counter()
            dt = now - last_time
            last_time = now

            with self.lock:
                carrier_ids = {f.carrier_id for f in self.flags.values() if f.state == "carried"}
                bomb_cells = set(self.bombs.keys())
                for player in self.players.values():
                    # a speed-changing disease takes priority over the flag-carry
                    # speed if a player somehow has both at once
                    if player.disease == "superspeed":
                        player.speed_override = settings.DISEASE_SUPERSPEED
                    elif player.disease == "slowdown":
                        player.speed_override = settings.DISEASE_SLOWDOWN
                    elif player.id in carrier_ids:
                        player.speed_override = settings.FLAG_CARRY_SPEED
                    else:
                        player.speed_override = None

                    if not self.match_started:
                        continue  # frozen in the lobby until the host starts the match

                    # ghost-walk passes through soft walls and bombs, but not the truly indestructible ones
                    blocked = self.hard_walls if player.disease == "walk_through_walls" else self.blocked_cells
                    if player.has_kick and player.disease != "walk_through_walls":
                        intended = player.intended_target()
                        bomb = self.bombs.get(intended)
                        if bomb is not None and bomb.move_dx == 0 and bomb.move_dy == 0:
                            bomb.move_dx = intended[0] - player.col
                            bomb.move_dy = intended[1] - player.row
                        blocked = blocked - bomb_cells

                    player.step(dt, blocked)

                self._update_kicked_bombs(dt)
                self._update_flags()
                self._update_power_ups(now)
                self._update_diseases(now)
                destroyed_walls = self._update_bombs(now)

                state = {
                    "type": "state",
                    "players": {
                        pid: {
                            "x": p.x, "y": p.y, "color": p.color, "team": p.team, "cells": p.occupied_cells(),
                            "bomb_capacity": p.bomb_capacity, "blast_range": p.blast_range,
                            "speed_bonus": p.speed_bonus, "disease": p.disease,
                            "has_kick": p.has_kick, "has_remote": p.has_remote,
                            "facing": p.facing, "is_moving": p.is_moving,
                        }
                        for pid, p in self.players.items()
                    },
                    "bombs": [
                        {
                            "x": b.x, "y": b.y,
                            "fuse_progress": min(1.0, (now - b.planted_at) / settings.BOMB_FUSE_SECONDS),
                        }
                        for b in self.bombs.values()
                    ],
                    "blasts": [
                        {"col": cell[0], "row": cell[1], "piece": piece}
                        for blast in self.active_blasts
                        for cell, piece in blast["pieces"].items()
                    ],
                    "power_ups": [
                        {"col": p.col, "row": p.row, "kind": p.kind} for p in self.power_ups.values()
                    ],
                    "flags": {
                        team: {"x": f.x, "y": f.y, "state": f.state, "carrier_id": f.carrier_id}
                        for team, f in self.flags.items()
                    },
                    "scores": self.scores,
                    "winner": self.winner,
                    "match_started": self.match_started,
                }
                clients_snapshot = dict(self.clients)

            dead = []
            for pid, sock in clients_snapshot.items():
                try:
                    send_message(sock, state)
                    for wall_cell in destroyed_walls:
                        send_message(sock, {"type": "wall_destroyed", "cell": wall_cell})
                except OSError:
                    dead.append(pid)

            if dead:
                with self.lock:
                    for pid in dead:
                        self.clients.pop(pid, None)
                        self.players.pop(pid, None)

            elapsed = time.perf_counter() - now
            time.sleep(max(0, settings.TICK_INTERVAL - elapsed))


if __name__ == "__main__":
    GameServer().start()
