"""Pygame client: networking menu (host / join) + free-move gameplay.

The client never decides its own position - it sends input intent to the
server and renders whatever state the server broadcasts back.
"""

import socket
import threading

import pygame

import server as server_module
import sprites as sprites_module
from shared import settings
from shared.protocol import send_message, MessageReader


class NetworkClient:
    def __init__(self):
        self.sock = None
        self.reader = None
        self.player_id = None
        self.team = None
        self.match_started = False
        self.players = {}
        self.prev_players = {}  # previous "state" snapshot, for interpolating render position
        self.state_tick_ms = 0  # local time (ms) the current snapshot arrived
        self.prev_tick_ms = 0  # local time (ms) the previous snapshot arrived
        self.bombs = []
        self.blasts = []
        self.power_ups = []
        self.recent_destructions = []  # (col, row) cells, drained each frame for the crumble animation
        self.flags = {}
        self.scores = {}
        self.winner = None
        self.hard_walls = set()
        self.soft_walls = set()
        self.lock = threading.Lock()
        self.connected = False
        self.error = None

    def connect(self, host, port):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(5)
            sock.connect((host, port))
            sock.settimeout(None)
        except OSError as e:
            self.error = str(e)
            return False

        self.sock = sock
        self.reader = MessageReader(sock)
        self.connected = True
        threading.Thread(target=self._listen_loop, daemon=True).start()
        return True

    def _listen_loop(self):
        while self.connected:
            try:
                messages = self.reader.read_messages()
            except OSError:
                break
            if messages is None:
                break
            for msg in messages:
                self._handle_message(msg)
        self.connected = False

    def _handle_message(self, msg):
        msg_type = msg["type"]
        if msg_type == "welcome":
            self.player_id = msg["player_id"]
            self.team = msg["team"]
            self.match_started = msg.get("match_started", False)
            self.hard_walls = {tuple(cell) for cell in msg["walls"]["hard"]}
            self.soft_walls = {tuple(cell) for cell in msg["walls"]["soft"]}
        elif msg_type == "state":
            now_ms = pygame.time.get_ticks()
            with self.lock:
                self.prev_players = self.players
                self.prev_tick_ms = self.state_tick_ms
                self.players = msg["players"]
                self.state_tick_ms = now_ms
                self.bombs = msg.get("bombs", [])
                self.blasts = msg.get("blasts", [])  # [{"col","row","piece"}, ...]
                self.power_ups = msg.get("power_ups", [])
                self.flags = msg.get("flags", {})
                self.scores = msg.get("scores", {})
                self.winner = msg.get("winner")
                self.match_started = msg.get("match_started", self.match_started)
        elif msg_type == "wall_destroyed":
            cell = tuple(msg["cell"])
            self.soft_walls.discard(cell)
            with self.lock:
                self.recent_destructions.append(cell)

    def drain_destructions(self):
        with self.lock:
            cells = list(self.recent_destructions)
            self.recent_destructions.clear()
        return cells

    def send_input(self, dx, dy):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "input", "dx": dx, "dy": dy})
        except OSError:
            self.connected = False

    def send_place_bomb(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "place_bomb"})
        except OSError:
            self.connected = False

    def send_detonate(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "detonate"})
        except OSError:
            self.connected = False

    def send_start_match(self):
        if not self.connected:
            return
        try:
            send_message(self.sock, {"type": "start_match"})
        except OSError:
            self.connected = False

    def render_state(self):
        """Snapshot of everything needed to draw a frame. Player positions are
        interpolated between the last two server updates (30/sec) so 60fps
        rendering doesn't visibly hold-then-jump between ticks.

        Renders RENDER_INTERP_DELAY_MS behind real-time so it's always
        blending between two already-confirmed positions (prev -> curr)
        rather than guessing ahead of the latest one - guessing ahead looks
        smooth until server timing jitters, then visibly snaps back to
        correct itself.
        """
        render_time = pygame.time.get_ticks() - settings.RENDER_INTERP_DELAY_MS
        with self.lock:
            curr, prev = self.players, self.prev_players
            curr_ms, prev_ms = self.state_tick_ms, self.prev_tick_ms
            bombs = list(self.bombs)
            blasts = list(self.blasts)
            power_ups = list(self.power_ups)
            flags = dict(self.flags)
            scores = dict(self.scores)
            winner = self.winner

        span = max(1, curr_ms - prev_ms)
        alpha = min(1.0, max(0.0, (render_time - prev_ms) / span))

        players = {}
        for pid, p in curr.items():
            before = prev.get(pid)
            if before is None:
                players[pid] = p
                continue
            blended = dict(p)
            blended["x"] = before["x"] + (p["x"] - before["x"]) * alpha
            blended["y"] = before["y"] + (p["y"] - before["y"]) * alpha
            players[pid] = blended

        return {
            "players": players,
            "bombs": bombs,
            "blasts": blasts,
            "power_ups": power_ups,
            "flags": flags,
            "scores": scores,
            "winner": winner,
        }


def start_local_server():
    game_server = server_module.GameServer(settings.DEFAULT_PORT)
    threading.Thread(target=game_server.start, daemon=True).start()
    game_server.ready.wait(timeout=3)  # avoid connecting before the socket is listening
    return game_server


def draw_flag(screen, x, y, color):
    pole_x = x + settings.CELL_SIZE * 0.35
    top_y = y + settings.CELL_SIZE * 0.12
    bottom_y = y + settings.CELL_SIZE * 0.88
    pygame.draw.line(screen, (225, 225, 225), (pole_x, top_y), (pole_x, bottom_y), 3)
    pygame.draw.polygon(screen, color, [
        (pole_x, top_y),
        (pole_x + settings.CELL_SIZE * 0.4, top_y + settings.CELL_SIZE * 0.14),
        (pole_x, top_y + settings.CELL_SIZE * 0.28),
    ])


def anim_frame(start_ticks, now_ticks, frame_ms, frame_count, loop=True):
    index = (now_ticks - start_ticks) // frame_ms
    return int(index) % frame_count if loop else min(int(index), frame_count - 1)


class Button:
    def __init__(self, rect, label):
        self.rect = pygame.Rect(rect)
        self.label = label

    def draw(self, screen, font):
        pygame.draw.rect(screen, (70, 70, 90), self.rect, border_radius=6)
        pygame.draw.rect(screen, (200, 200, 220), self.rect, width=2, border_radius=6)
        text = font.render(self.label, True, (240, 240, 240))
        screen.blit(text, text.get_rect(center=self.rect.center))

    def clicked(self, pos):
        return self.rect.collidepoint(pos)


def run():
    pygame.init()
    screen = pygame.display.set_mode((settings.FIELD_WIDTH, settings.FIELD_HEIGHT))
    pygame.display.set_caption("Bomberman CTF - Prototype")
    clock = pygame.time.Clock()
    font = pygame.font.SysFont("consolas", 24)
    small_font = pygame.font.SysFont("consolas", 18)
    sprites = sprites_module.load_sprites()  # must load after set_mode (needs a display surface)

    state = "menu"
    host_button = Button((300, 220, 200, 50), "Host Game")
    join_button = Button((300, 290, 200, 50), "Join Game")
    start_match_button = Button((300, 460, 200, 50), "Start Game")

    join_text = ""
    status_message = ""

    net = NetworkClient()
    last_sent = (0, 0)

    # client-side-only animation bookkeeping - none of this is networked state
    destroy_anim = {}  # (col, row) -> start_ticks, brick crumble one-shot
    blast_anim = {}  # (col, row) -> start_ticks, explosion frame timing
    death_anim = {}  # player_id -> start_ticks, death one-shot (triggered by a position-jump heuristic)
    last_positions = {}  # player_id -> (x, y), to detect the jump that means "this player respawned"

    running = True
    while running:
        clock.tick(60)

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False

            elif state == "menu" and event.type == pygame.MOUSEBUTTONDOWN:
                if host_button.clicked(event.pos):
                    start_local_server()
                    if net.connect("127.0.0.1", settings.DEFAULT_PORT):
                        state = "game" if net.match_started else "lobby"
                    else:
                        status_message = f"Failed to connect: {net.error}"
                elif join_button.clicked(event.pos):
                    state = "join_input"
                    join_text = ""

            elif state == "join_input" and event.type == pygame.KEYDOWN:
                if event.key == pygame.K_ESCAPE:
                    state = "menu"
                elif event.key == pygame.K_RETURN:
                    if net.connect(join_text.strip(), settings.DEFAULT_PORT):
                        state = "game" if net.match_started else "lobby"
                    else:
                        status_message = f"Failed to connect: {net.error}"
                        state = "menu"
                elif event.key == pygame.K_BACKSPACE:
                    join_text = join_text[:-1]
                elif event.unicode.isprintable():
                    join_text += event.unicode

            elif state == "lobby" and event.type == pygame.MOUSEBUTTONDOWN:
                if net.player_id == 1 and start_match_button.clicked(event.pos):
                    net.send_start_match()

            elif state == "game" and event.type == pygame.KEYDOWN:
                if event.key == pygame.K_SPACE:
                    net.send_place_bomb()
                elif event.key == pygame.K_e:
                    net.send_detonate()

        if state == "lobby":
            if not net.connected:
                status_message = "Disconnected from server."
                state = "menu"
            elif net.match_started:
                state = "game"

        if state == "game":
            if not net.connected:
                status_message = "Disconnected from server."
                state = "menu"
            else:
                keys = pygame.key.get_pressed()
                dx = (keys[pygame.K_d] or keys[pygame.K_RIGHT]) - (keys[pygame.K_a] or keys[pygame.K_LEFT])
                dy = (keys[pygame.K_s] or keys[pygame.K_DOWN]) - (keys[pygame.K_w] or keys[pygame.K_UP])
                if (dx, dy) != last_sent:
                    net.send_input(dx, dy)
                    last_sent = (dx, dy)

        screen.fill((30, 30, 40))

        if state == "menu":
            title = font.render("Bomberman CTF", True, (240, 240, 240))
            screen.blit(title, title.get_rect(center=(settings.FIELD_WIDTH // 2, 140)))
            host_button.draw(screen, font)
            join_button.draw(screen, font)
            if status_message:
                msg = small_font.render(status_message, True, (255, 150, 150))
                screen.blit(msg, msg.get_rect(center=(settings.FIELD_WIDTH // 2, 370)))

        elif state == "join_input":
            prompt = font.render("Enter host IP, then press Enter:", True, (240, 240, 240))
            screen.blit(prompt, prompt.get_rect(center=(settings.FIELD_WIDTH // 2, 220)))
            box = pygame.Rect(250, 260, 300, 40)
            pygame.draw.rect(screen, (50, 50, 65), box)
            pygame.draw.rect(screen, (200, 200, 220), box, width=2)
            text_surf = font.render(join_text, True, (240, 240, 240))
            screen.blit(text_surf, (box.x + 8, box.y + 8))
            hint = small_font.render("Esc to cancel  |  127.0.0.1 for same-PC testing", True, (180, 180, 180))
            screen.blit(hint, hint.get_rect(center=(settings.FIELD_WIDTH // 2, 320)))

        elif state == "lobby":
            title = font.render("Lobby", True, (240, 240, 240))
            screen.blit(title, title.get_rect(center=(settings.FIELD_WIDTH // 2, 100)))

            roster = net.render_state()["players"]
            list_top = 160
            if not roster:
                waiting = small_font.render("Waiting for players to connect...", True, (180, 180, 180))
                screen.blit(waiting, waiting.get_rect(center=(settings.FIELD_WIDTH // 2, list_top)))
            for i, (pid, p) in enumerate(sorted(roster.items(), key=lambda kv: int(kv[0]))):
                team = p.get("team", 0)
                team_name = settings.TEAM_NAMES[team]
                color = settings.TEAM_COLORS[team]
                you_tag = "  (you)" if str(pid) == str(net.player_id) else ""
                host_tag = "  [HOST]" if pid == "1" else ""
                line = small_font.render(f"Player {pid} - Team {team_name}{host_tag}{you_tag}", True, color)
                screen.blit(line, line.get_rect(center=(settings.FIELD_WIDTH // 2, list_top + i * 28)))

            if net.player_id == 1:
                start_match_button.draw(screen, font)
            else:
                waiting = small_font.render("Waiting for the host to start...", True, (200, 200, 200))
                screen.blit(waiting, waiting.get_rect(center=(settings.FIELD_WIDTH // 2, 475)))

        elif state == "game":
            now_ticks = pygame.time.get_ticks()

            for col in range(settings.GRID_COLS):
                for row in range(settings.GRID_ROWS):
                    screen.blit(sprites.ground, (col * settings.CELL_SIZE, row * settings.CELL_SIZE))

            all_walls = net.hard_walls | net.soft_walls
            for col, row in all_walls:
                shadow_row = row + 1
                if shadow_row < settings.GRID_ROWS and (col, shadow_row) not in all_walls:
                    screen.blit(sprites.ground_shadow, (col * settings.CELL_SIZE, shadow_row * settings.CELL_SIZE))

            pygame.draw.rect(screen, (20, 20, 25), (0, 0, settings.FIELD_WIDTH, settings.FIELD_HEIGHT), width=3)

            for col, row in net.hard_walls:
                screen.blit(sprites.block, (col * settings.CELL_SIZE, row * settings.CELL_SIZE))

            for col, row in net.soft_walls:
                screen.blit(sprites.brick, (col * settings.CELL_SIZE, row * settings.CELL_SIZE))

            for cell in net.drain_destructions():
                destroy_anim[cell] = now_ticks
            for cell, start in list(destroy_anim.items()):
                frame = anim_frame(start, now_ticks, settings.DESTROY_FRAME_MS, len(sprites.brick_destroy), loop=False)
                screen.blit(sprites.brick_destroy[frame], (cell[0] * settings.CELL_SIZE, cell[1] * settings.CELL_SIZE))
                if now_ticks - start > settings.DESTROY_FRAME_MS * len(sprites.brick_destroy):
                    destroy_anim.pop(cell, None)

            render_data = net.render_state()
            snapshot = render_data["players"]
            bombs = render_data["bombs"]
            blasts = render_data["blasts"]
            power_ups = render_data["power_ups"]
            flags = render_data["flags"]
            scores = render_data["scores"]
            winner = render_data["winner"]

            item_sprites = {"bomb": sprites.item_bomb, "fire": sprites.item_fire, "speed": sprites.item_speed}
            for power_up in power_ups:
                kind = power_up["kind"]
                pos = (power_up["col"] * settings.CELL_SIZE, power_up["row"] * settings.CELL_SIZE)
                if kind in item_sprites:
                    screen.blit(item_sprites[kind], pos)
                else:
                    center = (pos[0] + settings.CELL_SIZE // 2, pos[1] + settings.CELL_SIZE // 2)
                    color = settings.POWER_UP_COLORS[kind]
                    pygame.draw.circle(screen, color, center, int(settings.CELL_SIZE * 0.3))
                    pygame.draw.circle(screen, (255, 255, 255), center, int(settings.CELL_SIZE * 0.3), width=2)
                    label = small_font.render(settings.POWER_UP_LABELS[kind], True, (20, 20, 20))
                    screen.blit(label, label.get_rect(center=center))

            bomb_frame = anim_frame(0, now_ticks, settings.BOMB_FRAME_MS, len(sprites.bomb))
            for bomb in bombs:
                screen.blit(sprites.bomb[bomb_frame], (bomb["x"], bomb["y"]))

            seen_blast_cells = set()
            for blast in blasts:
                cell = (blast["col"], blast["row"])
                seen_blast_cells.add(cell)
                start = blast_anim.setdefault(cell, now_ticks)
                frame = anim_frame(start, now_ticks, settings.EXPLOSION_FRAME_MS, 8, loop=False)
                pos = (cell[0] * settings.CELL_SIZE, cell[1] * settings.CELL_SIZE)
                piece = blast["piece"]
                if piece == "start":
                    screen.blit(sprites.explosion_start[frame], pos)
                elif piece in ("mid_h", "mid_v"):
                    screen.blit(sprites.explosion_middle[piece][frame], pos)
                else:
                    screen.blit(sprites.explosion_end[piece][frame], pos)
            for cell in list(blast_anim.keys()):
                if cell not in seen_blast_cells:
                    blast_anim.pop(cell, None)

            for team_key, flag in flags.items():
                team = int(team_key)
                draw_flag(screen, flag["x"], flag["y"], settings.TEAM_COLORS[team])

            highlight = pygame.Surface((settings.CELL_SIZE, settings.CELL_SIZE), pygame.SRCALPHA)
            for p in snapshot.values():
                color = tuple(p["color"])
                for cell_col, cell_row in p.get("cells", []):
                    highlight.fill((*color, 60))
                    screen.blit(highlight, (cell_col * settings.CELL_SIZE, cell_row * settings.CELL_SIZE))

            blink_phase = (now_ticks // 150) % 4  # color -> white -> color -> black -> repeat
            sprite_h = sprites.player_walk[0]["down"][0].get_height()
            y_offset = settings.CELL_SIZE - sprite_h  # anchors feet to the tile's bottom edge

            for pid, p in snapshot.items():
                team = p.get("team", 0)
                prev_pos = last_positions.get(pid, (p["x"], p["y"]))
                jumped = abs(p["x"] - prev_pos[0]) + abs(p["y"] - prev_pos[1]) > settings.DEATH_JUMP_THRESHOLD
                last_positions[pid] = (p["x"], p["y"])
                if jumped:
                    death_anim[pid] = now_ticks

                pos = (p["x"], p["y"] + y_offset)

                if pid in death_anim:
                    frames = sprites.player_death[team]
                    elapsed = now_ticks - death_anim[pid]
                    frame = min(int(elapsed // settings.DEATH_FRAME_MS), len(frames) - 1)
                    screen.blit(frames[frame], pos)
                    if elapsed > settings.DEATH_FRAME_MS * len(frames):
                        death_anim.pop(pid, None)
                    continue

                direction_frames = sprites.player_walk[team][p.get("facing", "down")]
                frame = anim_frame(0, now_ticks, settings.WALK_FRAME_MS, len(direction_frames)) if p.get("is_moving") else 0
                sprite = direction_frames[frame]
                if p.get("disease") and blink_phase in (1, 3):
                    sprite = sprite.copy()
                    if blink_phase == 1:
                        sprite.fill((255, 255, 255), special_flags=pygame.BLEND_RGB_ADD)
                    else:
                        sprite.fill((255, 255, 255), special_flags=pygame.BLEND_RGB_SUB)
                screen.blit(sprite, pos)
                if str(pid) == str(net.player_id):
                    pygame.draw.rect(screen, (255, 255, 255), (*pos, sprite.get_width(), sprite.get_height()), width=2)

            team_name = settings.TEAM_NAMES[net.team] if net.team is not None else "?"
            hud = small_font.render(
                f"Player {net.player_id} (Team {team_name})  |  Space: bomb  |  E: detonate", True, (220, 220, 220)
            )
            screen.blit(hud, (10, 10))

            own = snapshot.get(str(net.player_id))
            if own:
                stats_text = f"Bombs: {own['bomb_capacity']}   Range: {own['blast_range']}   Speed: +{own['speed_bonus']:.1f}"
                if own.get("has_kick"):
                    stats_text += "   KICK"
                if own.get("has_remote"):
                    stats_text += "   REMOTE"
                stats = small_font.render(stats_text, True, (200, 220, 200))
                screen.blit(stats, (10, 32))

            score_text = "   ".join(
                f"{settings.TEAM_NAMES[int(team_key)]}: {score}" for team_key, score in scores.items()
            )
            score_hud = font.render(score_text, True, (240, 240, 240))
            screen.blit(score_hud, score_hud.get_rect(midtop=(settings.FIELD_WIDTH // 2, 8)))

            if winner is not None:
                banner = font.render(f"{settings.TEAM_NAMES[winner]} TEAM WINS!", True, (255, 230, 120))
                banner_rect = banner.get_rect(center=(settings.FIELD_WIDTH // 2, settings.FIELD_HEIGHT // 2))
                pygame.draw.rect(screen, (20, 20, 25), banner_rect.inflate(24, 16))
                screen.blit(banner, banner_rect)

        pygame.display.flip()

    pygame.quit()


if __name__ == "__main__":
    run()
